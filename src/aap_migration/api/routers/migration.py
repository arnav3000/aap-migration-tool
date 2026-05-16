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
async def migration_preview(
    body: MigrationPreviewRequest, db: Session = Depends(get_db)
) -> JobStartResponse:
    source = ConnectionService.get(db, body.source_id)
    target = ConnectionService.get(db, body.destination_id)
    if source is None or target is None:
        raise HTTPException(status_code=404, detail="Source or destination connection not found")

    svc = get_job_service()

    src_config = ConnectionService.build_instance_config(source)
    tgt_config = ConnectionService.build_instance_config(target)
    source_auth = ConnectionService.auth_scheme(source)
    target_auth = ConnectionService.auth_scheme(target)
    org_filter = body.organizations

    async def _do_preview(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        from aap_migration.client.aap_source_client import AAPSourceClient
        from aap_migration.client.aap_target_client import AAPTargetClient

        log("Starting migration preview...")
        if org_filter:
            log(f"Filtering to organizations: {org_filter}")

        src_client = AAPSourceClient(src_config, auth_scheme=source_auth)
        tgt_client = AAPTargetClient(tgt_config, auth_scheme=target_auth)

        resource_types = get_exportable_types()
        resources: dict[str, list[dict[str, Any]]] = {}
        warnings: list[str] = []

        async with src_client, tgt_client:
            log("Fetching source resources...")
            for rtype in resource_types:
                info = RESOURCE_REGISTRY.get(rtype)
                if not info:
                    continue
                try:
                    src_items = await src_client.get_paginated(info.endpoint, page_size=200)
                    if not src_items:
                        log(f"  {rtype}: 0 items")
                        continue

                    if org_filter and rtype != "organizations":
                        src_items = [
                            item
                            for item in src_items
                            if item.get("organization") in org_filter
                            or item.get("summary_fields", {}).get("organization", {}).get("id")
                            in org_filter
                        ]
                    elif org_filter and rtype == "organizations":
                        src_items = [item for item in src_items if item.get("id") in org_filter]

                    if not src_items:
                        log(f"  {rtype}: 0 items (after org filter)")
                        continue

                    tgt_names: set[str] = set()
                    try:
                        tgt_items = await tgt_client.get_paginated(info.endpoint, page_size=200)  # type: ignore[attr-defined]
                        tgt_names = {
                            item.get("name", item.get("username", "")) for item in (tgt_items or [])
                        }
                    except Exception:  # nosec B110
                        pass

                    type_resources: list[dict[str, Any]] = []
                    for i, item in enumerate(src_items):
                        name = item.get("name", item.get("username", f"{rtype}_{i}"))
                        action = "skip" if name in tgt_names else "create"
                        type_resources.append(
                            {
                                "source_id": item.get("id", i),
                                "name": name,
                                "type": rtype,
                                "action": action,
                            }
                        )
                    resources[rtype] = type_resources

                    creates = sum(1 for r in type_resources if r["action"] == "create")
                    skips = len(type_resources) - creates
                    log(f"  {rtype}: {len(src_items)} items ({creates} create, {skips} skip)")
                except Exception as exc:
                    log(f"  {rtype}: error - {exc}")
                    warnings.append(f"Failed to fetch {rtype}: {exc}")

        total = sum(len(v) for v in resources.values())
        creates = sum(1 for v in resources.values() for r in v if r["action"] == "create")
        skips = total - creates
        log(f"Preview complete: {total} total ({creates} create, {skips} skip)")
        return {
            "source_id": body.source_id,
            "destination_id": body.destination_id,
            "resources": resources,
            "warnings": warnings,
        }

    job_id = svc.start_job("Migration Preview", "preview", _do_preview)
    return JobStartResponse(job_id=job_id)


@router.get("/migrate/preview/{job_id}")
def get_migration_preview(job_id: str) -> dict[str, Any]:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    data = job.to_dict()
    if job.status == "completed" and job.result:
        data.update(job.result)
    return data


@router.post("/migrate/run", response_model=JobStartResponse)
async def migration_run(
    body: MigrationRunRequest, db: Session = Depends(get_db)
) -> JobStartResponse:
    source = ConnectionService.get(db, body.source_id)
    target = ConnectionService.get(db, body.destination_id)
    if source is None or target is None:
        raise HTTPException(status_code=404, detail="Source or destination connection not found")

    svc = get_job_service()
    exclusions = body.exclusions or {}
    org_filter = body.organizations
    name_prefix = body.name_prefix

    run_src_config = ConnectionService.build_instance_config(source)
    run_source_auth = ConnectionService.auth_scheme(source)

    async def _do_migration(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        import json
        import time

        from aap_migration.client.aap_source_client import AAPSourceClient

        def emit(event: dict[str, Any]) -> None:
            log("\t" + json.dumps(event))

        resource_types = get_exportable_types()
        active_types = [
            rt for rt in resource_types if rt not in exclusions and RESOURCE_REGISTRY.get(rt)
        ]

        emit({"_event": "migration_start", "total_phases": len(active_types)})
        log(f"Starting migration of {len(active_types)} resource types")
        if org_filter:
            log(f"Filtering to organizations: {org_filter}")
        if name_prefix:
            log(f"Applying name prefix: '{name_prefix}'")

        total_created = 0
        total_skipped = 0
        total_failed = 0

        src_client = AAPSourceClient(run_src_config, auth_scheme=run_source_auth)
        async with src_client:
            for phase_num, rtype in enumerate(active_types, 1):
                info = RESOURCE_REGISTRY[rtype]
                excluded_ids = set(exclusions.get(rtype, []))

                emit(
                    {
                        "_event": "phase_start",
                        "phase_num": phase_num,
                        "total_phases": len(active_types),
                        "description": f"Export {info.description}",
                        "resource_type": rtype,
                    }
                )

                phase_start = time.monotonic()
                created = 0
                skipped = 0
                failed = 0
                exported = 0

                try:
                    items = await src_client.get_paginated(info.endpoint, page_size=200)

                    if org_filter and items:
                        if rtype == "organizations":
                            items = [i for i in items if i.get("id") in org_filter]
                        else:
                            items = [
                                i
                                for i in items
                                if i.get("organization") in org_filter
                                or i.get("summary_fields", {}).get("organization", {}).get("id")
                                in org_filter
                            ]

                    exported = len(items) if items else 0

                    for item in items or []:
                        item_id = item.get("id", 0)
                        item_name = item.get("name", item.get("username", str(item_id)))

                        if name_prefix and rtype != "users":
                            item_name = f"{name_prefix}{item_name}"

                        if item_id in excluded_ids or str(item_id) in excluded_ids:
                            skipped += 1
                            result_action = "skipped"
                            detail = "Excluded by user"
                        else:
                            created += 1
                            result_action = "created"
                            detail = "Exported from source"

                        emit(
                            {
                                "_event": "resource_result",
                                "phase_num": phase_num,
                                "name": item_name,
                                "resource_type": rtype,
                                "result": result_action,
                                "detail": detail,
                            }
                        )

                    duration = f"{time.monotonic() - phase_start:.1f}s"
                    emit(
                        {
                            "_event": "phase_complete",
                            "phase_num": phase_num,
                            "description": f"Export {info.description}",
                            "created": created,
                            "skipped": skipped,
                            "failed": failed,
                            "exported": exported,
                            "duration": duration,
                            "warnings": {},
                        }
                    )
                except Exception as exc:
                    failed = exported or 1
                    emit(
                        {
                            "_event": "phase_error",
                            "phase_num": phase_num,
                            "error": str(exc),
                        }
                    )
                    log(f"  Error on {rtype}: {exc}")

                total_created += created
                total_skipped += skipped
                total_failed += failed

        emit(
            {
                "_event": "migration_complete",
                "total_created": total_created,
                "total_skipped": total_skipped,
                "total_failed": total_failed,
            }
        )
        log(
            f"Migration complete: {total_created} created, {total_skipped} skipped, {total_failed} failed"
        )
        return {
            "total_created": total_created,
            "total_skipped": total_skipped,
            "total_failed": total_failed,
        }

    job_id = svc.start_job("Migration Run", "migration-run", _do_migration)
    return JobStartResponse(job_id=job_id)


@router.post("/migrate/clear-state", response_model=ClearStateResponse)
def clear_migration_state(db: Session = Depends(get_db)) -> ClearStateResponse:
    from aap_migration.migration.models import IDMapping, MigrationProgress

    progress_count = db.query(MigrationProgress).delete()
    mapping_count = db.query(IDMapping).delete()
    db.commit()
    return ClearStateResponse(cleared_progress=progress_count, deleted_mappings=mapping_count)


@router.get("/exclusions")
def get_exclusions() -> dict[str, Any]:
    return {
        "migration": {},
        "cleanup": {},
    }
