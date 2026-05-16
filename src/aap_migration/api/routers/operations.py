"""Cleanup and export operations as background jobs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, get_job_service
from aap_migration.api.schemas import JobStartResponse
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.job_service import Job

router = APIRouter()


@router.post("/connections/{conn_id}/cleanup", response_model=JobStartResponse)
async def run_cleanup(conn_id: str, db: Session = Depends(get_db)) -> JobStartResponse:
    conn = ConnectionService.get(db, conn_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    svc = get_job_service()

    async def _do_cleanup(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        import asyncio

        log(f"Starting cleanup for connection {conn.name} ({conn.url})")
        client = ConnectionService.build_target_client(conn)
        total_deleted = 0
        total_errors = 0

        async with client:
            from aap_migration.resources import get_exportable_types

            resource_types = get_exportable_types()
            for rtype in reversed(resource_types):
                log(f"Cleaning up {rtype}...")
                try:
                    resources = await asyncio.wait_for(client.list_resources(rtype), timeout=60.0)
                    if not resources:
                        log(f"  No {rtype} found, skipping")
                        continue

                    ids = [int(r["id"]) for r in resources if r.get("id") is not None]
                    log(f"  Found {len(ids)} {rtype} to delete")
                    deleted = 0
                    errors = 0

                    batch_size = 10
                    for i in range(0, len(ids), batch_size):
                        batch = ids[i : i + batch_size]

                        async def _delete(rt: str, rid: int) -> bool:
                            try:
                                await asyncio.wait_for(
                                    client.delete_resource(rt, rid),
                                    timeout=30.0,
                                )
                                return True
                            except Exception:
                                return False

                        results = await asyncio.gather(*[_delete(rtype, rid) for rid in batch])
                        deleted += sum(1 for r in results if r)
                        errors += sum(1 for r in results if not r)
                        if i + batch_size < len(ids):
                            log(f"  {rtype}: {deleted}/{len(ids)} deleted...")

                    log(f"  Deleted {deleted} {rtype}" + (f" ({errors} errors)" if errors else ""))
                    total_deleted += deleted
                    total_errors += errors
                except TimeoutError:
                    log(f"  Timeout listing {rtype}, skipping")
                    total_errors += 1
                except Exception as exc:
                    log(f"  Error cleaning {rtype}: {exc}")
                    total_errors += 1

        log(f"Cleanup complete: {total_deleted} deleted, {total_errors} errors")
        return {"deleted": total_deleted, "errors": total_errors}

    job_id = svc.start_job(f"Cleanup {conn.name}", "cleanup", _do_cleanup)
    return JobStartResponse(job_id=job_id)


@router.post("/connections/{conn_id}/export", response_model=JobStartResponse)
async def run_export(conn_id: str, db: Session = Depends(get_db)) -> JobStartResponse:
    conn = ConnectionService.get(db, conn_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    svc = get_job_service()

    async def _do_export(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        log(f"Starting export from {conn.name} ({conn.url})")
        client = ConnectionService.build_source_client(conn)
        async with client:
            from aap_migration.resources import get_exportable_types

            resource_types = get_exportable_types()
            exported: dict[str, int] = {}
            for rtype in resource_types:
                log(f"Exporting {rtype}...")
                try:
                    resources = await client.get_paginated(f"{rtype}/", page_size=200)
                    exported[rtype] = len(resources) if resources else 0
                    log(f"  Exported {exported[rtype]} {rtype}")
                except Exception as exc:
                    log(f"  Error exporting {rtype}: {exc}")
                    exported[rtype] = 0
        log("Export complete")
        return {"status": "completed", "exported": exported}

    job_id = svc.start_job(f"Export {conn.name}", "export", _do_export)
    return JobStartResponse(job_id=job_id)
