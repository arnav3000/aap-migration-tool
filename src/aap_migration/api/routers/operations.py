"""Cleanup, export, and selective migration operations as background jobs."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, get_db_url, get_job_service
from aap_migration.api.schemas import JobStartResponse, SelectiveMigrateRequest
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


def _fk_id(data: dict[str, Any], field: str) -> int | None:
    """Read a foreign-key ID from a resource, falling back to summary_fields."""
    value = data.get(field)
    if value is not None:
        return int(value)
    summary = data.get("summary_fields", {}).get(field, {})
    if isinstance(summary, dict):
        sid = summary.get("id")
        return int(sid) if sid is not None else None
    return None


async def _resolve_jt_dependencies(
    client: Any,
    jt_ids: list[int],
    log: Callable[[str], None],
) -> tuple[dict[str, set[int]], list[dict[str, Any]]]:
    """Walk selected JTs and collect all transitive dependency source IDs.

    Returns (deps_by_type, jt_data_list) where deps_by_type maps resource type
    names to sets of source IDs that must be migrated before the JTs.
    """
    deps: dict[str, set[int]] = {}

    def _add(rtype: str, rid: int | None) -> None:
        if rid is not None:
            deps.setdefault(rtype, set()).add(rid)

    jt_data_list: list[dict[str, Any]] = []

    for jt_id in jt_ids:
        try:
            jt = await client.get_resource_by_id("job_templates", jt_id)
        except Exception as exc:
            log(f"  Warning: could not fetch job_template/{jt_id}: {exc}")
            continue
        jt_data_list.append(jt)

        sf = jt.get("summary_fields", {})
        _add("organizations", jt.get("organization") or sf.get("organization", {}).get("id"))
        _add("projects", jt.get("project") or sf.get("project", {}).get("id"))
        _add("inventories", jt.get("inventory") or sf.get("inventory", {}).get("id"))
        _add(
            "execution_environments",
            jt.get("execution_environment") or sf.get("execution_environment", {}).get("id"),
        )
        _add("credentials", jt.get("webhook_credential"))

        for label in sf.get("labels", {}).get("results", []):
            _add("labels", label.get("id"))

        try:
            creds = await client.get_job_template_credentials(jt_id)
            for cred in creds:
                _add("credentials", cred.get("id"))
        except Exception:  # nosec B110
            pass

    project_ids = list(deps.get("projects", set()))
    for pid in project_ids:
        try:
            proj = await client.get_resource_by_id("projects", pid)
            _add("organizations", _fk_id(proj, "organization"))
            _add("credentials", proj.get("credential"))
            _add("execution_environments", proj.get("default_environment"))
        except Exception as exc:  # nosec B110
            log(f"  Warning: could not fetch project/{pid}: {exc}")

    inventory_ids = list(deps.get("inventories", set()))
    for inv_id in inventory_ids:
        try:
            inv = await client.get_resource_by_id("inventories", inv_id)
            _add("organizations", _fk_id(inv, "organization"))
        except Exception as exc:  # nosec B110
            log(f"  Warning: could not fetch inventory/{inv_id}: {exc}")

    cred_ids = list(deps.get("credentials", set()))
    for cred_id in cred_ids:
        try:
            cred = await client.get_resource_by_id("credentials", cred_id)
            _add("credential_types", cred.get("credential_type"))
            _add("organizations", _fk_id(cred, "organization"))
        except Exception as exc:  # nosec B110
            log(f"  Warning: could not fetch credential/{cred_id}: {exc}")

    ee_ids = list(deps.get("execution_environments", set()))
    for ee_id in ee_ids:
        try:
            ee = await client.get_resource_by_id("execution_environments", ee_id)
            _add("organizations", _fk_id(ee, "organization"))
        except Exception as exc:  # nosec B110
            log(f"  Warning: could not fetch execution_environment/{ee_id}: {exc}")

    deps.pop("job_templates", None)

    total = sum(len(v) for v in deps.values())
    log(f"  Resolved {total} dependency objects across {len(deps)} resource types")
    return deps, jt_data_list


@router.post("/selective-migrate", response_model=JobStartResponse)
async def selective_migrate(
    body: SelectiveMigrateRequest,
    db: Session = Depends(get_db),
) -> JobStartResponse:
    """Migrate selected job templates and their transitive dependencies."""
    source_conn = ConnectionService.get(db, body.source_id)
    if source_conn is None:
        raise HTTPException(status_code=404, detail="Source connection not found")

    dest_conn = ConnectionService.get(db, body.destination_id)
    if dest_conn is None:
        raise HTTPException(status_code=404, detail="Destination connection not found")

    src_cfg = ConnectionService.build_instance_config(source_conn)
    src_auth = ConnectionService._auth_scheme(source_conn)
    dest_cfg = ConnectionService.build_instance_config(dest_conn)
    dest_auth = ConnectionService._auth_scheme(dest_conn)
    jt_ids = body.job_template_ids
    force_update = body.force_update
    db_url = get_db_url()

    svc = get_job_service()

    async def _do_selective(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        import time

        from aap_migration.client.aap_source_client import AAPSourceClient
        from aap_migration.client.aap_target_client import AAPTargetClient
        from aap_migration.config import MigrationConfig, StateConfig
        from aap_migration.migration.importer import create_importer
        from aap_migration.migration.state import MigrationState
        from aap_migration.migration.transformer import SkipResourceError, create_transformer
        from aap_migration.resources import RESOURCE_REGISTRY, get_migration_order

        def emit(event: dict[str, Any]) -> None:
            log("\t" + json.dumps(event))

        src_client = AAPSourceClient(src_cfg, auth_scheme=src_auth)
        target_client = AAPTargetClient(dest_cfg, auth_scheme=dest_auth)
        migration_config = MigrationConfig(
            source=src_cfg,
            target=dest_cfg,
            state=StateConfig(db_path=db_url),
        )
        state = MigrationState(migration_config.state)

        totals = {"created": 0, "skipped": 0, "failed": 0}

        async with src_client, target_client:
            log(f"Resolving dependencies for {len(jt_ids)} job template(s)...")
            deps, jt_data = await _resolve_jt_dependencies(src_client, jt_ids, log)

            migration_order = [
                rt
                for rt in get_migration_order()
                if rt in deps and RESOURCE_REGISTRY[rt].has_importer
            ]
            migration_order.append("job_templates")
            num_phases = len(migration_order)

            if force_update:
                from aap_migration.migration.models import IDMapping, MigrationProgress

                all_force_types = list(deps.keys()) + ["job_templates"]
                cleared_progress = 0
                reset_mappings = 0
                try:
                    with state._lock:
                        from aap_migration.migration.database import get_session

                        with get_session(state.database_url) as session:
                            for rt in all_force_types:
                                source_id_list = (
                                    [jt.get("id") for jt in jt_data if jt.get("id") is not None]
                                    if rt == "job_templates"
                                    else list(deps.get(rt, set()))
                                )
                                if not source_id_list:
                                    continue
                                cleared_progress += (
                                    session.query(MigrationProgress)
                                    .filter(
                                        MigrationProgress.resource_type == rt,
                                        MigrationProgress.source_id.in_(source_id_list),
                                    )
                                    .delete(synchronize_session=False)
                                )
                                reset_mappings += (
                                    session.query(IDMapping)
                                    .filter(
                                        IDMapping.resource_type == rt,
                                        IDMapping.source_id.in_(source_id_list),
                                    )
                                    .update(
                                        {"target_id": None, "target_name": None},
                                        synchronize_session=False,
                                    )
                                )
                            session.commit()
                    log(
                        "Force mode: cleared "
                        f"{cleared_progress} progress record(s), "
                        f"reset {reset_mappings} mapping(s)"
                    )
                except Exception as exc:
                    log(f"Warning: failed to clear prior state: {exc}")

            emit({"_event": "migration_start", "total_phases": num_phases})

            for phase_num, rtype in enumerate(migration_order, 1):
                info = RESOURCE_REGISTRY[rtype]
                emit(
                    {
                        "_event": "phase_start",
                        "phase_num": phase_num,
                        "total_phases": num_phases,
                        "description": info.description,
                        "resource_type": rtype,
                    }
                )

                phase_start = time.monotonic()
                created = 0
                skipped = 0
                failed = 0
                exported = 0

                if rtype == "job_templates":
                    resources_to_import = jt_data
                else:
                    source_ids = deps[rtype]
                    resources_to_import = []
                    for sid in source_ids:
                        try:
                            res = await src_client.get_resource_by_id(rtype, sid)
                            resources_to_import.append(res)
                        except Exception as exc:
                            log(f"  Warning: failed to fetch {rtype}/{sid}: {exc}")
                            failed += 1

                transformer = (
                    create_transformer(
                        resource_type=rtype,
                        dry_run=False,
                        state=state,
                        defer_project_sync=False,
                    )
                    if info.has_transformer
                    else None
                )
                importer = create_importer(
                    resource_type=rtype,
                    client=target_client,
                    state=state,
                    performance_config=migration_config.performance,
                    resource_mappings=migration_config.resource_mappings,
                )

                for resource in resources_to_import:
                    source_id = resource.get("id")
                    if source_id is None:
                        continue

                    if state.is_migrated(rtype, int(source_id)) and state.has_source_mapping(
                        rtype, int(source_id)
                    ):
                        skipped += 1
                        res_name = resource.get("name", resource.get("username", str(source_id)))
                        log(
                            f"  Skipping {rtype}/{source_id} ({res_name}): "
                            "already migrated (enable Force update to re-import)"
                        )
                        emit(
                            {
                                "_event": "resource_result",
                                "phase_num": phase_num,
                                "name": res_name,
                                "resource_type": rtype,
                                "result": "skipped",
                                "detail": "Already migrated",
                            }
                        )
                        continue

                    if transformer:
                        try:
                            resource = transformer.transform_resource(
                                resource_type=rtype,
                                data=resource,
                                validate=True,
                            )
                        except SkipResourceError as exc:
                            skipped += 1
                            res_name = resource.get(
                                "name", resource.get("username", str(source_id))
                            )
                            log(f"  Skipping {rtype}/{source_id} ({res_name}): {exc}")
                            continue
                        except Exception:
                            failed += 1
                            continue
                    elif not state.has_source_mapping(rtype, int(source_id)):
                        # Types without a transformer (orgs, credential_types, etc.)
                        # never register mappings during transform — do it here so
                        # downstream types can validate dependencies.
                        state.create_source_mapping(
                            resource_type=rtype,
                            source_id=int(source_id),
                            source_name=resource.get("name", resource.get("username")),
                        )

                    exported += 1

                    try:
                        result = await importer.import_resource(
                            resource_type=rtype,
                            source_id=int(source_id),
                            data=resource,
                        )
                        res_name = resource.get("name", resource.get("username", str(source_id)))
                        if result:
                            target_id = result.get("id")
                            if target_id is not None and not state.get_mapped_id(
                                rtype, int(source_id)
                            ):
                                state.save_id_mapping(
                                    resource_type=rtype,
                                    source_id=int(source_id),
                                    target_id=int(target_id),
                                    source_name=res_name,
                                    target_name=result.get("name", result.get("username")),
                                )
                            if result.get("_skipped"):
                                skipped += 1
                                result_action = "skipped"
                                detail = "Duplicate exists in target"
                            else:
                                created += 1
                                result_action = "created"
                                detail = ""
                            emit(
                                {
                                    "_event": "resource_result",
                                    "phase_num": phase_num,
                                    "name": res_name,
                                    "resource_type": rtype,
                                    "result": result_action,
                                    "detail": detail,
                                }
                            )
                        else:
                            skipped += 1
                    except Exception as exc:
                        failed += 1
                        emit(
                            {
                                "_event": "resource_result",
                                "phase_num": phase_num,
                                "name": resource.get(
                                    "name",
                                    resource.get("username", str(source_id)),
                                ),
                                "resource_type": rtype,
                                "result": "failed",
                                "detail": str(exc)[:200],
                            }
                        )

                duration = f"{time.monotonic() - phase_start:.1f}s"
                emit(
                    {
                        "_event": "phase_complete",
                        "phase_num": phase_num,
                        "description": info.description,
                        "created": created,
                        "updated": 0,
                        "skipped": skipped,
                        "failed": failed,
                        "exported": exported,
                        "duration": duration,
                        "warnings": {},
                    }
                )
                totals["created"] += created
                totals["skipped"] += skipped
                totals["failed"] += failed

        emit(
            {
                "_event": "migration_complete",
                "total_created": totals["created"],
                "total_updated": 0,
                "total_skipped": totals["skipped"],
                "total_failed": totals["failed"],
            }
        )
        return totals

    jt_count = len(jt_ids)
    job_id = svc.start_job(
        f"Selective migrate {jt_count} JT(s) → {dest_conn.name}",
        "selective-migration",
        _do_selective,
    )
    return JobStartResponse(job_id=job_id)
