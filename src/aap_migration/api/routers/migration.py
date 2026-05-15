"""Migration preview, run, state management endpoints."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, get_job_service
from aap_migration.api.schemas import (
    ClearStateResponse,
    JobStartResponse,
    MigrationPreviewRequest,
    MigrationRunRequest,
)
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.job_service import Job
from aap_migration.resources import RESOURCE_REGISTRY, get_exportable_types

router = APIRouter()


@router.post("/migrate/preview", response_model=JobStartResponse)
def migration_preview(
    body: MigrationPreviewRequest, db: Session = Depends(get_db)
) -> JobStartResponse:
    source = ConnectionService.get(db, body.source_id)
    target = ConnectionService.get(db, body.destination_id)
    if source is None or target is None:
        raise HTTPException(status_code=404, detail="Source or destination connection not found")

    svc = get_job_service()

    source_url = source.url
    source_token = source.token
    source_verify = source.verify_ssl
    source_timeout = source.timeout
    target_url = target.url

    async def _do_preview(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        from aap_migration.client.aap_source_client import AAPSourceClient
        from aap_migration.config import AAPInstanceConfig

        log("Starting migration preview (prep)...")
        src_config = AAPInstanceConfig(
            url=source_url,
            token=source_token,
            verify_ssl=source_verify,
            timeout=source_timeout,
        )

        src_client = AAPSourceClient(src_config)

        resource_types = get_exportable_types()
        preview: dict[str, Any] = {
            "resource_types": {},
            "source_url": source_url,
            "target_url": target_url,
        }

        async with src_client:
            log("Discovering source resources...")
            for rtype in resource_types:
                info = RESOURCE_REGISTRY.get(rtype)
                if not info:
                    continue
                try:
                    data = await src_client.get_paginated(info.endpoint, page_size=1)
                    count = len(data) if data else 0
                except Exception:
                    count = 0
                preview["resource_types"][rtype] = {
                    "count": count,
                    "description": info.description,
                }
                log(f"  {rtype}: {count}")

        log("Preview complete")
        return preview

    job_id = svc.start_job("Migration Preview", "preview", _do_preview)
    return JobStartResponse(job_id=job_id)


@router.get("/migrate/preview/{job_id}")
def get_migration_preview(job_id: str) -> dict[str, Any]:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@router.post("/migrate/run", response_model=JobStartResponse)
def migration_run(body: MigrationRunRequest, db: Session = Depends(get_db)) -> JobStartResponse:
    source = ConnectionService.get(db, body.source_id)
    target = ConnectionService.get(db, body.destination_id)
    if source is None or target is None:
        raise HTTPException(status_code=404, detail="Source or destination connection not found")

    svc = get_job_service()
    exclusions = body.exclusions or {}

    source_url = source.url
    source_token = source.token
    source_verify = source.verify_ssl
    source_timeout = source.timeout

    async def _do_migration(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        import tempfile
        from pathlib import Path

        from aap_migration.config import AAPInstanceConfig

        log("Starting migration run...")

        work_dir = Path(tempfile.mkdtemp(prefix="aap-bridge-migration-"))
        for subdir in ("exports", "xformed", "schemas"):
            (work_dir / subdir).mkdir(parents=True, exist_ok=True)

        log(f"Work directory: {work_dir}")
        log(f"Exclusions: {exclusions}")

        src_config = AAPInstanceConfig(
            url=source_url,
            token=source_token,
            verify_ssl=source_verify,
            timeout=source_timeout,
        )

        resource_types = get_exportable_types()
        log(f"Resource types to migrate: {len(resource_types)}")

        log("Phase 1: Export")
        from aap_migration.client.aap_source_client import AAPSourceClient

        src_client = AAPSourceClient(src_config)
        async with src_client:
            for rtype in resource_types:
                info = RESOURCE_REGISTRY.get(rtype)
                if not info:
                    continue
                log(f"  Exporting {rtype}...")
                try:
                    resources = await src_client.get_paginated(info.endpoint, page_size=200)
                    count = len(resources) if resources else 0
                    log(f"    {count} resources")
                except Exception as exc:
                    log(f"    Error: {exc}")

        log("Phase 2: Transform")
        log("  (Transform step placeholder — run CLI for full pipeline)")

        log("Phase 3: Import")
        log("  (Import step placeholder — run CLI for full pipeline)")

        log("Migration run complete (partial — use CLI for full export/transform/import)")
        return {"status": "completed", "work_dir": str(work_dir)}

    job_id = svc.start_job("Migration Run", "migration", _do_migration)
    return JobStartResponse(job_id=job_id)


@router.post("/migrate/clear-state", response_model=ClearStateResponse)
def clear_migration_state(db: Session = Depends(get_db)) -> ClearStateResponse:
    from aap_migration.migration.models import IDMapping, MigrationProgress

    progress_count = db.query(MigrationProgress).delete()
    mapping_count = db.query(IDMapping).delete()
    db.commit()
    return ClearStateResponse(cleared_progress=progress_count, deleted_mappings=mapping_count)


@router.get("/exclusions")
def get_exclusions() -> dict[str, dict[str, str]]:
    types = get_exportable_types()
    return {
        rtype: {
            "description": RESOURCE_REGISTRY[rtype].description
            if rtype in RESOURCE_REGISTRY
            else rtype,
        }
        for rtype in types
    }
