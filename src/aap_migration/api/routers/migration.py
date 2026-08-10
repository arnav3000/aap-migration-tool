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
    MigrationExportRequest,
    MigrationImportRequest,
    MigrationPreviewRequest,
    MigrationRunRequest,
    MigrationTransformRequest,
)
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.job_service import Job, JobStatus
from aap_migration.resources import RESOURCE_REGISTRY, get_default_exclusions

# High-cardinality types: preview uses count + first page only (avoid loading full catalogs).
_HIGH_CARDINALITY_PREVIEW_TYPES = frozenset({"hosts", "host_inventory_memberships"})
_PREVIEW_SAMPLE_SIZE = 50

router = APIRouter()


def _output_dir_from_job(job_id: str, key: str = "output_dir") -> str:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Prior job not found")
    if not job.result or not job.result.get(key):
        raise HTTPException(status_code=400, detail=f"Prior job missing {key}")
    return str(job.result[key])


@router.post("/migrate/export", response_model=JobStartResponse)
async def migration_export(
    body: MigrationExportRequest, db: Session = Depends(get_db)
) -> JobStartResponse:
    source = ConnectionService.get(db, body.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source connection not found")

    svc = get_job_service()
    src_config = ConnectionService.build_instance_config(source)
    source_auth = ConnectionService._auth_scheme(source)
    output_dir = body.output_dir or "exports"
    db_url = get_db_url()

    async def _do_export(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        from aap_migration.client.aap_source_client import AAPSourceClient
        from aap_migration.config import StateConfig
        from aap_migration.migration.runner import run_disk_export
        from aap_migration.migration.state import MigrationState

        client = AAPSourceClient(src_config, auth_scheme=source_auth)
        state = MigrationState(
            StateConfig(db_path=db_url),
            migration_id=f"export-{body.source_id}",
            source_key=body.source_id,
        )
        async with client:
            return await run_disk_export(
                client,
                state,
                output_dir,
                resource_types=body.resource_types,
                records_per_file=body.records_per_file,
                resume=body.resume,
                log=log,
            )

    job_id = svc.start_job(f"Export {source.name}", "export", _do_export)
    return JobStartResponse(job_id=job_id)


@router.post("/migrate/transform", response_model=JobStartResponse)
async def migration_transform(
    body: MigrationTransformRequest, db: Session = Depends(get_db)
) -> JobStartResponse:
    input_dir = body.input_dir
    if body.export_job_id:
        input_dir = _output_dir_from_job(body.export_job_id, "output_dir")
    assert input_dir is not None

    svc = get_job_service()
    output_dir = body.output_dir or "xformed"
    db_url = get_db_url()
    target_config = None
    target_auth = None

    if body.destination_id:
        target = ConnectionService.get(db, body.destination_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Destination connection not found")
        target_config = ConnectionService.build_instance_config(target)
        target_auth = ConnectionService._auth_scheme(target)

    async def _do_transform(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        from aap_migration.client.aap_target_client import AAPTargetClient
        from aap_migration.config import StateConfig
        from aap_migration.migration.runner import run_disk_transform
        from aap_migration.migration.state import MigrationState

        state = MigrationState(StateConfig(db_path=db_url))
        if target_config is not None:
            client = AAPTargetClient(target_config, auth_scheme=target_auth or "Bearer")
            async with client:
                return await run_disk_transform(
                    state,
                    input_dir,
                    output_dir,
                    resource_types=body.resource_types,
                    target_client=client,
                    defer_project_sync=body.defer_project_sync,
                    log=log,
                )
        return await run_disk_transform(
            state,
            input_dir,
            output_dir,
            resource_types=body.resource_types,
            defer_project_sync=body.defer_project_sync,
            log=log,
        )

    job_id = svc.start_job("Transform exports", "transform", _do_transform)
    return JobStartResponse(job_id=job_id)


@router.post("/migrate/import", response_model=JobStartResponse)
async def migration_import(
    body: MigrationImportRequest, db: Session = Depends(get_db)
) -> JobStartResponse:
    source = ConnectionService.get(db, body.source_id)
    target = ConnectionService.get(db, body.destination_id)
    if source is None or target is None:
        raise HTTPException(status_code=404, detail="Source or destination connection not found")

    input_dir = body.input_dir
    if body.transform_job_id:
        input_dir = _output_dir_from_job(body.transform_job_id, "output_dir")
    assert input_dir is not None

    svc = get_job_service()
    src_config = ConnectionService.build_instance_config(source)
    tgt_config = ConnectionService.build_instance_config(target)
    source_auth = ConnectionService._auth_scheme(source)
    target_auth = ConnectionService._auth_scheme(target)
    name_prefix = (body.name_prefix or "").strip()
    org_filter = body.organizations
    db_url = get_db_url()

    async def _do_import(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        from aap_migration.client.aap_source_client import AAPSourceClient
        from aap_migration.client.aap_target_client import AAPTargetClient
        from aap_migration.config import StateConfig
        from aap_migration.migration.runner import run_disk_import
        from aap_migration.migration.state import MigrationState
        from aap_migration.resources import get_fully_supported_types

        resource_types = body.resource_types or get_fully_supported_types()
        state = MigrationState(
            StateConfig(db_path=db_url),
            migration_id=f"import-{body.source_id}-{body.destination_id}",
            source_key=body.source_id,
        )
        src_client = AAPSourceClient(src_config, auth_scheme=source_auth)
        tgt_client = AAPTargetClient(tgt_config, auth_scheme=target_auth)
        async with src_client, tgt_client:
            return await run_disk_import(
                src_client,
                tgt_client,
                state,
                input_dir,
                resource_types,
                name_prefix=name_prefix,
                org_ids=list(org_filter) if org_filter else None,
                dry_run=body.dry_run,
                log=log,
            )

    job_id = svc.start_job(f"Import to {target.name}", "import", _do_import)
    return JobStartResponse(job_id=job_id)


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
        from aap_migration.resources import get_fully_supported_types
        from aap_migration.utils.naming import should_apply_name_prefix

        log("Starting migration preview (scanning target to seed ID mappings)...")
        if org_filter:
            log(f"Filtering to organizations: {org_filter}")
        if name_prefix:
            log(f"Applying name prefix for match: '{name_prefix}'")

        src_client = AAPSourceClient(src_config, auth_scheme=source_auth)
        tgt_client = AAPTargetClient(tgt_config, auth_scheme=target_auth)
        state = MigrationState(
            StateConfig(db_path=db_url),
            migration_id=f"preview-{body.source_id}-{body.destination_id}",
            source_key=source_key,
        )

        resources: dict[str, list[dict[str, Any]]] = {}
        host_counts: dict[str, int] = {}
        group_counts: dict[str, int] = {}
        warnings: list[str] = []
        bootstrap_totals = {"mapped": 0, "unmatched": 0}

        # Dependency order so org/project maps exist before JT/credential matching.
        resource_types = get_fully_supported_types()

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
                    if rtype in _HIGH_CARDINALITY_PREVIEW_TYPES:
                        total_on_source = await src_client.get_count(info.endpoint)
                        src_items = await src_client.get_paginated(
                            info.endpoint, page_size=_PREVIEW_SAMPLE_SIZE
                        )
                        preview_note = f"{total_on_source} total, showing first {len(src_items)}"
                    else:
                        total_on_source = None
                        preview_note = None
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
                        mapped_target_id = (
                            state.get_mapped_id(rtype, source_id_int) if already else None
                        )
                        type_resources.append(
                            {
                                "source_id": source_id,
                                "name": display_name,
                                "type": rtype,
                                "action": "skip" if already else "create",
                                "target_id": mapped_target_id,
                                "dest_id": mapped_target_id,
                            }
                        )

                    if rtype == "inventories":
                        for item in src_items:
                            inv_name = item.get("name", "")
                            if inv_name:
                                host_counts[inv_name] = item.get("total_hosts", 0)
                                group_counts[inv_name] = item.get("total_groups", 0)

                    resources[rtype] = type_resources
                    creates = sum(1 for r in type_resources if r["action"] == "create")
                    skips = len(type_resources) - creates
                    if preview_note:
                        log(
                            f"  {rtype}: {preview_note} "
                            f"({creates} create, {skips} already on target in sample)"
                        )
                    else:
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
            "host_counts": host_counts,
            "group_counts": group_counts,
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
        from aap_migration.migration.runner import (
            _build_source_contexts,
            _handle_credential_pause,
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
                    from aap_migration.migration.runner import _handle_credential_pause

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
    return get_default_exclusions()
