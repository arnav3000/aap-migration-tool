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

        from sqlalchemy import text

        log(f"Starting cleanup for connection {conn.name} ({conn.url})")
        client = ConnectionService.build_target_client(conn)
        total_deleted = 0
        total_errors = 0
        cleared_types: list[str] = []

        DELETE_CONCURRENCY = 10

        async def _delete_one(rtype: str, rid: int, sem: asyncio.Semaphore) -> bool:
            async with sem:
                await client.delete_resource(rtype, rid)
                return True

        async with client:
            from aap_migration.resources import RESOURCE_REGISTRY, get_cleanup_order

            SKIP_CLEANUP = {
                "settings",
                "system_job_templates",
                "instances",
                "instance_groups",
                "host_inventory_memberships",
            }

            resource_types = get_cleanup_order()
            for rtype in resource_types:
                info = RESOURCE_REGISTRY.get(rtype)
                if not info or not info.has_importer:
                    continue
                if rtype in SKIP_CLEANUP:
                    log(f"Cleaning up {rtype}... skipped (not deletable)")
                    continue

                log(f"Cleaning up {rtype}...")
                try:
                    resources = await client.list_resources(rtype)
                    if not resources:
                        log(f"  No {rtype} found, skipping")
                        continue

                    ids = [int(r["id"]) for r in resources if r.get("id") is not None]
                    log(f"  Found {len(ids)} {rtype} to delete")
                    deleted = 0
                    errors = 0
                    sem = asyncio.Semaphore(DELETE_CONCURRENCY)

                    batch: list[tuple[int, asyncio.Task[bool]]] = []
                    for rid in ids:
                        task = asyncio.create_task(_delete_one(rtype, rid, sem))
                        batch.append((rid, task))

                    for rid, task in batch:
                        try:
                            await asyncio.wait_for(task, timeout=60.0)
                            deleted += 1
                        except TimeoutError:
                            errors += 1
                            task.cancel()
                            log(f"  Timeout deleting {rtype}/{rid}")
                        except Exception as exc:
                            errors += 1
                            detail = str(exc)[:120]
                            log(f"  Failed to delete {rtype}/{rid}: {detail}")

                        if deleted > 0 and deleted % 50 == 0:
                            log(f"  {rtype}: {deleted}/{len(ids)} deleted...")

                    log(f"  Deleted {deleted} {rtype}" + (f" ({errors} errors)" if errors else ""))
                    total_deleted += deleted
                    total_errors += errors
                    if deleted > 0:
                        cleared_types.append(rtype)
                except Exception as exc:
                    log(f"  Error cleaning {rtype}: {exc}")
                    total_errors += 1

        if cleared_types:
            log("Clearing migration state for deleted resource types...")
            try:
                from aap_migration.api.dependencies import get_app_state

                app_state = get_app_state()
                session = app_state.db_session_factory()
                try:
                    for rtype in cleared_types:
                        session.execute(
                            text("DELETE FROM id_mappings WHERE resource_type = :rt"),
                            {"rt": rtype},
                        )
                        session.execute(
                            text("DELETE FROM migration_progress WHERE resource_type = :rt"),
                            {"rt": rtype},
                        )
                    session.commit()
                    log(f"  Cleared state for: {', '.join(cleared_types)}")
                except Exception as exc:
                    session.rollback()
                    log(f"  Warning: failed to clear migration state: {exc}")
                finally:
                    session.close()
            except Exception as exc:
                log(f"  Warning: could not access migration state DB: {exc}")

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
