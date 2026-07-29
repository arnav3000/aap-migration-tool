"""Migration preview, run, state management endpoints."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, get_db_url, get_job_service
from aap_migration.api.schemas import (
    ClearStateResponse,
    JobStartResponse,
    MigrationPreviewRequest,
    MigrationRunRequest,
)
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.job_service import Job, JobStatus
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
    source_auth = ConnectionService._auth_scheme(source)
    target_auth = ConnectionService._auth_scheme(target)
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
    if job.status == JobStatus.COMPLETED and job.result:
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
    name_prefix = body.name_prefix or ""

    run_src_config = ConnectionService.build_instance_config(source)
    run_tgt_config = ConnectionService.build_instance_config(target)
    run_source_auth = ConnectionService._auth_scheme(source)
    run_target_auth = ConnectionService._auth_scheme(target)
    db_url = get_db_url()
    source_name = getattr(source, "name", None) or run_src_config.url

    async def _do_migration(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        # Reuse planner ETL helpers so quick-migrate actually creates resources
        # on the target (export → transform → import), including name_prefix.
        from aap_migration.api.routers.planner import (
            _build_source_contexts,
            _migrate_resource_type,
            _run_cac_org_update,
        )
        from aap_migration.resources import (
            apply_host_membership_resource_cascade,
            get_fully_supported_types,
        )

        def emit(event: dict[str, Any]) -> None:
            log("\t" + json.dumps(event))

        preview_resources: dict[str, list[dict[str, Any]]] = {}
        preview_job = svc.get_job(body.job_id)
        if preview_job and preview_job.result:
            preview_resources = preview_job.result.get("resources") or {}
        else:
            # Some job backends stash preview payload on metadata instead of result.
            meta = getattr(preview_job, "job_metadata", None) if preview_job else None
            if isinstance(meta, dict):
                preview_resources = meta.get("resources") or {}

        full_resource_order = get_fully_supported_types()
        resource_order = apply_host_membership_resource_cascade(
            full_resource_order,
            exclusions=exclusions,
            preview_resources=preview_resources or None,
        )
        org_ids = list(org_filter) if org_filter else []

        emit({"_event": "migration_start", "total_phases": len(resource_order)})
        log(f"Starting migration of {len(resource_order)} resource types")
        if (
            "host_inventory_memberships" in full_resource_order
            and "host_inventory_memberships" not in resource_order
        ):
            log("Skipping host_inventory_memberships because hosts were excluded")
        if org_ids:
            log(f"Filtering to organizations: {org_ids}")
        if name_prefix:
            log(f"Applying name prefix: '{name_prefix}'")

        source_configs: list[dict[str, Any]] = [
            {
                "url": run_src_config.url,
                "token": run_src_config.token,
                "verify_ssl": run_src_config.verify_ssl,
                "timeout": run_src_config.timeout,
                "name_prefix": name_prefix,
                "connection_name": source_name,
                "org_ids": org_ids,
                "auth_scheme": run_source_auth,
                "source_key": body.source_id,
                "connection_id": body.source_id,
            }
        ]

        _, target_client, sources = _build_source_contexts(
            source_configs,
            run_tgt_config,
            run_target_auth,
            db_url,
        )
        for src in sources:
            if exclusions:
                src["excluded_ids"] = exclusions
            if preview_resources:
                src["preview_resources"] = preview_resources

        totals = {"created": 0, "skipped": 0, "failed": 0, "updated": 0}
        created_creds: list[dict[str, str]] = []
        CRED_PAUSE_AFTER = {"credentials", "credential_input_sources"}

        for phase_num, rtype in enumerate(resource_order, 1):
            emit(
                {
                    "_event": "phase_start",
                    "phase_num": phase_num,
                    "total_phases": len(resource_order),
                    "description": RESOURCE_REGISTRY[rtype].description,
                    "resource_type": rtype,
                }
            )

            created, skipped, failed, _exported = await _migrate_resource_type(
                rtype,
                sources,
                target_client,
                phase_num,
                emit,
                log,
                created_creds,
            )
            totals["created"] += created
            totals["skipped"] += skipped
            totals["failed"] += failed

            # Same pause-and-patch workflow as planner: credentials are created
            # with temporary secrets; projects/JTs need real secrets on the target
            # before they can attach and sync.
            if rtype in CRED_PAUSE_AFTER:
                remaining = CRED_PAUSE_AFTER - set(
                    resource_order[: resource_order.index(rtype) + 1]
                )
                if not remaining and created_creds:
                    from aap_migration.api.routers.planner import _handle_credential_pause

                    await _handle_credential_pause(
                        job,
                        svc,
                        created_creds,
                        sources,
                        "",  # non-planner: in-memory resume only
                        "",
                        emit,
                        log,
                    )

        log("CaC pass: re-patching organizations with final references...")
        totals["updated"] += await _run_cac_org_update(
            sources,
            target_client,
            len(resource_order),
            emit,
            log,
        )

        emit(
            {
                "_event": "migration_complete",
                "total_created": totals["created"],
                "total_updated": totals["updated"],
                "total_skipped": totals["skipped"],
                "total_failed": totals["failed"],
            }
        )
        log(
            f"Migration complete: {totals['created']} created, "
            f"{totals['skipped']} skipped, {totals['failed']} failed"
        )
        return {
            "total_created": totals["created"],
            "total_skipped": totals["skipped"],
            "total_failed": totals["failed"],
            "total_updated": totals["updated"],
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
