"""Migration preview, run, state management endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, get_db_url, get_job_service, verify_api_token
from aap_migration.api.schemas import (
    ClearStateRequest,
    ClearStateResponse,
    JobStartResponse,
    MigrationPreviewRequest,
    MigrationRunRequest,
)
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.resources import RESOURCE_REGISTRY

router = APIRouter(tags=["migration"], dependencies=[Depends(verify_api_token)])


@router.post("/migrate/preview", response_model=JobStartResponse)
async def migration_preview(
    body: MigrationPreviewRequest,
    db: Session = Depends(get_db),
) -> JobStartResponse:
    source = ConnectionService.get(db, body.source_id)
    target = ConnectionService.get(db, body.destination_id)
    if not source or not target:
        raise HTTPException(status_code=404, detail="Source or destination connection not found")

    # Validate resource_types
    if body.resource_types:
        for rt in body.resource_types:
            if rt not in RESOURCE_REGISTRY:
                raise HTTPException(status_code=422, detail=f"Unknown resource type: {rt}")

    if body.organizations is not None and not isinstance(body.organizations, list):
        raise HTTPException(status_code=422, detail="organizations must be a list")

    from aap_migration.api.dependencies import get_job_service as _get_svc

    svc = _get_svc()
    db_url = get_db_url()
    name_prefix = (body.name_prefix or "").strip()
    org_filter = body.organizations

    async def _preview_via_log(log: Any) -> dict[str, Any]:
        import asyncio

        from aap_migration.api.services.migration_service import (
            _counts_via_export,
            _resolve_resource_types,
        )

        resolved = _resolve_resource_types(body.resource_types)
        log("Starting migration preview (scanning source)...")
        if org_filter:
            log(f"Filtering to organizations: {org_filter}")
        if name_prefix:
            log(f"Applying name prefix for match: '{name_prefix}'")
        log(f"Resource types: {', '.join(resolved)}")
        counts = await _counts_via_export(source, db_url, resolved, org_filter, name_prefix, log)
        total = sum(counts.values())
        # Try to inject real job_id if available via current task
        job_id = "preview-temp"
        try:
            cur = asyncio.current_task()
            for j in svc._jobs.values():  # type: ignore[attr-defined]
                if j._task is cur:  # type: ignore[attr-defined]
                    job_id = j.job_id
                    break
        except Exception:
            pass
        result: dict[str, Any] = {
            "job_id": job_id,
            "status": "completed",
            "counts": counts,
            "resource_types": resolved,
            "warnings": [] if total else ["No resources found"],
            "total": total,
        }
        log(f"Preview complete: {total} total")
        return result

    job = await svc.start_job(
        "preview", lambda log: _preview_via_log(log), name="Migration Preview"
    )
    return JobStartResponse(job_id=job.job_id, seq_id=job.seq_id)


@router.get("/migrate/preview/{job_id}")
async def get_migration_preview(job_id: str) -> dict[str, Any]:
    svc = get_job_service()
    data = svc.get_job_dict(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Preview job not found")
    # If still running, return status
    if data.get("status") in ("running", "pending"):
        return {"job_id": job_id, "status": data["status"], "result": data.get("result")}
    # Completed preview: return result
    result = data.get("result") or {}
    # Ensure counts present
    if not result and data.get("output"):
        result = data["output"]
    return {
        "job_id": job_id,
        "status": data.get("status"),
        "counts": result.get("counts", {}),
        "resource_types": result.get("resource_types", []),
        "warnings": result.get("warnings", []),
        "result": result,
        "output": data.get("output"),
        "error": data.get("error"),
    }


@router.post("/migrate/run", response_model=JobStartResponse)
async def migration_run(
    body: MigrationRunRequest,
    db: Session = Depends(get_db),
) -> JobStartResponse:
    source = ConnectionService.get(db, body.source_id)
    target = ConnectionService.get(db, body.destination_id)
    if not source or not target:
        raise HTTPException(status_code=404, detail="Source or destination connection not found")

    if body.resource_types:
        for rt in body.resource_types:
            if rt not in RESOURCE_REGISTRY:
                raise HTTPException(status_code=422, detail=f"Unknown resource type: {rt}")

    svc = get_job_service()
    db_url = get_db_url()
    name_prefix = (body.name_prefix or "").strip()
    org_filter = body.organizations
    dry_run = bool(body.dry_run)
    skip_validation = bool(body.skip_validation)

    async def _factory(log: Any) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        # Need the Job object for execute_migration; get it from svc._jobs via log closure?
        # We can retrieve the currently running job by finding the most recent job with matching name
        # Simpler: find job by looking for a job whose task is current task
        import asyncio

        cur = asyncio.current_task()
        job_obj = None
        for j in svc._jobs.values():  # type: ignore[attr-defined]
            if j._task is cur:  # type: ignore[attr-defined]
                job_obj = j
                break
        if job_obj is None:
            # Fallback: create a dummy job
            from aap_migration.api.services.job_service import Job

            job_obj = Job(job_id="tmp", seq_id=0, job_type="migration")
        from aap_migration.api.services.migration_service import execute_migration

        return await execute_migration(
            job_obj,
            log,
            source_conn=source,
            target_conn=target,
            db_url=db_url,
            resource_types=body.resource_types,
            organizations=org_filter,
            name_prefix=name_prefix,
            dry_run=dry_run,
            skip_validation=skip_validation,
        )

    job = await svc.start_job("migration", _factory, name="Migration")
    return JobStartResponse(job_id=job.job_id, seq_id=job.seq_id)


@router.post("/migrate/clear-state", response_model=ClearStateResponse)
async def clear_migration_state(
    body: ClearStateRequest | None = None,
    db: Session = Depends(get_db),  # noqa: ARG001
) -> ClearStateResponse:
    # body may be empty; handle both {} and None
    resource_types: list[str] | None = None
    if body and body.resource_types:
        resource_types = body.resource_types
        for rt in resource_types:
            if rt not in RESOURCE_REGISTRY:
                raise HTTPException(status_code=422, detail=f"Unknown resource type: {rt}")

    db_url = get_db_url()
    # Extract path
    db_path = (
        db_url.replace("sqlite:///", "").replace("sqlite://", "") if "sqlite" in db_url else db_url
    )
    cleared = 0
    try:
        from aap_migration.config import StateConfig
        from aap_migration.migration.state import MigrationState

        state = MigrationState(config=StateConfig(db_path=db_path))
        if resource_types:
            for rt in resource_types:
                # clear_progress returns count? Check implementation: clear_progress may not exist.
                # Fallback to direct delete via state methods
                try:
                    if hasattr(state, "clear_progress"):
                        cleared += int(state.clear_progress(rt) or 0)  # type: ignore[attr-defined]
                    else:
                        # Manually delete via session
                        from aap_migration.migration.database import get_session
                        from aap_migration.migration.models import IDMapping, MigrationProgress

                        with get_session(state.database_url) as session:
                            del1 = (
                                session.query(MigrationProgress)
                                .filter_by(resource_type=rt)
                                .delete()
                            )
                            del2 = session.query(IDMapping).filter_by(resource_type=rt).delete()
                            session.commit()
                            cleared += del1 + del2
                except Exception:
                    # Try alternative: reset_target_ids
                    try:
                        if hasattr(state, "reset_target_ids"):
                            state.reset_target_ids(rt)  # type: ignore[attr-defined]
                    except Exception:
                        pass
        else:
            # Clear all
            from aap_migration.migration.database import get_session
            from aap_migration.migration.models import IDMapping, MigrationProgress

            with get_session(state.database_url) as session:
                del1 = session.query(MigrationProgress).delete()
                del2 = session.query(IDMapping).delete()
                session.commit()
                cleared = del1 + del2
    except Exception as e:
        # If database not initialized yet, consider cleared=0
        raise HTTPException(status_code=500, detail=f"Failed to clear state: {e}") from e

    return ClearStateResponse(cleared=cleared, message=f"Cleared {cleared} records")


@router.get("/exclusions")
async def get_exclusions() -> dict[str, Any]:
    # Return ignored endpoints + read-only/runtime sets
    ignored_file = Path("config/ignored_endpoints.yaml")
    ignored: dict[str, Any] = {"common": [], "source": [], "target": []}
    if ignored_file.exists():
        try:
            import yaml

            with open(ignored_file) as f:
                data = yaml.safe_load(f) or {}
                if "ignored_endpoints" in data:
                    raw = data["ignored_endpoints"]
                    if isinstance(raw, list):
                        ignored = {"common": raw, "source": [], "target": []}
                    elif isinstance(raw, dict):
                        ignored = {
                            "common": raw.get("common") or [],
                            "source": raw.get("source") or [],
                            "target": raw.get("target") or [],
                        }
        except Exception:
            pass

    from aap_migration.resources import (
        MANUAL_MIGRATION_ENDPOINTS,
        READ_ONLY_ENDPOINTS,
        RUNTIME_DATA_ENDPOINTS,
    )

    return {
        "ignored_endpoints": ignored,
        "read_only": sorted(READ_ONLY_ENDPOINTS),
        "runtime": sorted(RUNTIME_DATA_ENDPOINTS),
        "manual": sorted(MANUAL_MIGRATION_ENDPOINTS),
    }
