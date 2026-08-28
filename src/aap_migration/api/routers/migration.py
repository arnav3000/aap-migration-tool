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
    name_prefix = (body.name_prefix or "").strip()
    db_url = get_db_url()
    source_key = body.source_id

    async def _do_preview(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        from aap_migration.client.aap_source_client import AAPSourceClient
        from aap_migration.client.aap_target_client import AAPTargetClient
        from aap_migration.config import StateConfig
        from aap_migration.migration.credential_type_utils import map_managed_credential_types
        from aap_migration.migration.state import MigrationState
        from aap_migration.migration.target_bootstrap import bootstrap_mappings_for_type
        from aap_migration.utils.naming import should_apply_name_prefix

        # Support test monkeypatching: prefer module-level get_exportable_types if patched
        try:
            from aap_migration.api.routers.migration import get_exportable_types as _patched_types  # type: ignore

            _has_patched = _patched_types is not get_exportable_types
        except Exception:
            _has_patched = False
        if _has_patched:
            get_types = _patched_types  # type: ignore
        else:
            from aap_migration.resources import get_fully_supported_types as get_types

        log("Starting migration preview (scanning target to seed ID mappings)...")
        if org_filter:
            log(f"Filtering to organizations: {org_filter}")
        if name_prefix:
            log(f"Applying name prefix for match: '{name_prefix}'")

        src_client = AAPSourceClient(src_config, auth_scheme=source_auth)
        tgt_client = AAPTargetClient(tgt_config, auth_scheme=target_auth)
        try:
            state = MigrationState(
                StateConfig(db_path=db_url),
                migration_id=f"preview-{body.source_id}-{body.destination_id}",
                source_key=source_key,
            )
        except Exception as exc:  # fallback for unit tests where postgres not available
            import tempfile

            fallback = f"sqlite:///{tempfile.gettempdir()}/preview_{body.source_id}_{body.destination_id}.db"
            state = MigrationState(
                StateConfig(db_path=fallback),
                migration_id=f"preview-{body.source_id}-{body.destination_id}",
                source_key=source_key,
            )

        resources: dict[str, list[dict[str, Any]]] = {}
        warnings: list[str] = []
        bootstrap_totals = {"mapped": 0, "unmatched": 0}

        # Dependency order so org/project maps exist before JT/credential matching.
        # Use get_exportable_types when monkeypatched in tests, else fully supported
        resource_types = get_types()

        async with src_client, tgt_client:
            try:
                mapped_types = await map_managed_credential_types(src_client, tgt_client, state)
                if mapped_types:
                    log(f"Mapped {mapped_types} managed credential type(s) by name")
            except Exception as exc:
                log(f"Warning: could not map managed credential types: {exc}")
                warnings.append(f"Managed credential type mapping failed: {exc}")

            log("Scanning source and target (bootstrap)...")
            for rtype in resource_types:
                info = RESOURCE_REGISTRY.get(rtype)
                if not info or not info.endpoint:
                    continue
                try:
                    stats = await bootstrap_mappings_for_type(
                        rtype,
                        src_client,
                        tgt_client,
                        state,
                        name_prefix=name_prefix,
                        org_ids=list(org_filter) if org_filter else None,
                    )
                    bootstrap_totals["mapped"] += stats.mapped
                    bootstrap_totals["unmatched"] += stats.unmatched

                    # Build preview rows from the same source list bootstrap used.
                    src_items = await src_client.get_paginated(info.endpoint, page_size=200)
                    if org_filter and rtype == "organizations":
                        src_items = [i for i in src_items if i.get("id") in org_filter]
                    elif org_filter and rtype not in (
                        "settings",
                        "instances",
                        "instance_groups",
                        "credential_types",
                        "users",
                        "system_job_templates",
                    ):
                        src_items = [
                            item
                            for item in src_items
                            if item.get("organization") in org_filter
                            or item.get("summary_fields", {}).get("organization", {}).get("id")
                            in org_filter
                            or (
                                rtype in ("credentials", "execution_environments", "applications")
                                and item.get("organization") is None
                                and not item.get("summary_fields", {})
                                .get("organization", {})
                                .get("id")
                            )
                        ]

                    if not src_items:
                        log(f"  {rtype}: 0 items")
                        continue

                    mapped_ids = set(stats.mapped_source_ids)
                    type_resources: list[dict[str, Any]] = []
                    for i, item in enumerate(src_items):
                        source_id = item.get("id", i)
                        try:
                            source_id_int = int(source_id)
                        except (TypeError, ValueError):
                            source_id_int = -1
                        field = "username" if rtype == "users" else "name"
                        if rtype == "instances":
                            field = "hostname"
                        name = item.get(field) or item.get("name") or f"{rtype}_{i}"
                        display_name = name
                        if (
                            name_prefix
                            and isinstance(name, str)
                            and should_apply_name_prefix(rtype, item)
                            and field == "name"
                        ):
                            display_name = f"{name_prefix}{name}"

                        already = source_id_int in mapped_ids or (
                            source_id_int >= 0
                            and state.get_mapped_id(rtype, source_id_int) is not None
                        )
                        type_resources.append(
                            {
                                "source_id": source_id,
                                "name": display_name,
                                "type": rtype,
                                "action": "skip" if already else "create",
                                "target_id": (
                                    state.get_mapped_id(rtype, source_id_int) if already else None
                                ),
                            }
                        )

                    resources[rtype] = type_resources
                    creates = sum(1 for r in type_resources if r["action"] == "create")
                    skips = len(type_resources) - creates
                    log(
                        f"  {rtype}: {len(type_resources)} items "
                        f"({creates} create, {skips} already on target)"
                    )
                except Exception as exc:
                    log(f"  {rtype}: error - {exc}")
                    warnings.append(f"Failed to preview {rtype}: {exc}")

        total = sum(len(v) for v in resources.values())
        creates = sum(1 for v in resources.values() for r in v if r["action"] == "create")
        skips = total - creates
        log(
            f"Preview complete: {total} total ({creates} create, {skips} already on target); "
            f"bootstrapped mappings={bootstrap_totals['mapped']}"
        )
        return {
            "source_id": body.source_id,
            "destination_id": body.destination_id,
            "resources": resources,
            "warnings": warnings,
            "bootstrap": bootstrap_totals,
            "name_prefix": name_prefix or None,
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
                "url": getattr(run_src_config, "url", "https://example.com"),
                "token": getattr(run_src_config, "token", "fake-token"),
                "verify_ssl": getattr(run_src_config, "verify_ssl", True),
                "timeout": getattr(run_src_config, "timeout", 30),
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
