"""Cleanup, export, and selective migration operations as background jobs."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

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


def _clear_selective_resource_state(
    state: Any,
    resource_type: str,
    source_id: int,
) -> None:
    """Drop stale progress/mapping so a missing target resource can be re-imported."""
    from aap_migration.migration.database import get_session
    from aap_migration.migration.models import IDMapping, MigrationProgress

    with state._lock:
        with get_session(state.database_url) as session:
            session.query(MigrationProgress).filter(
                MigrationProgress.resource_type == resource_type,
                MigrationProgress.source_id == source_id,
            ).delete(synchronize_session=False)
            session.query(IDMapping).filter(
                IDMapping.resource_type == resource_type,
                IDMapping.source_id == source_id,
            ).update(
                {"target_id": None, "target_name": None},
                synchronize_session=False,
            )
            session.commit()


def _maybe_apply_name_prefix(
    resource_type: str,
    resource: dict[str, Any],
    name_prefix: str,
) -> None:
    """Prepend optional name prefix after transform, before import."""
    if not name_prefix:
        return
    from aap_migration.utils.naming import apply_name_prefix

    apply_name_prefix(resource_type, resource, name_prefix)


async def _should_skip_migrated_resource(
    state: Any,
    target_client: Any,
    resource_type: str,
    source_id: int,
    log: Callable[[str], None],
) -> bool:
    """Return True only when state and target AAP both confirm the resource exists."""
    if not state.is_migrated(resource_type, source_id):
        return False

    target_id = state.get_mapped_id(resource_type, source_id)
    if target_id is None:
        _clear_selective_resource_state(state, resource_type, source_id)
        return False

    try:
        await target_client.get(f"{resource_type}/{target_id}/")
        return True
    except Exception:
        log(
            f"  {resource_type}/{source_id}: recorded as migrated but target "
            f"{target_id} is missing — re-importing"
        )
        _clear_selective_resource_state(state, resource_type, source_id)
        return False


async def _list_inventory_sources_for_inventory(
    client: Any,
    inv_id: int,
    log: Callable[[str], None],
) -> list[dict[str, Any]]:
    """List inventory sources for an inventory, with nested-endpoint fallback."""
    try:
        sources = await client.get_inventory_sources(params={"inventory": inv_id})
        if sources:
            return cast(list[dict[str, Any]], sources)
    except Exception as exc:  # nosec B110
        log(f"  Warning: inventory_sources query failed for inventory/{inv_id}: {exc}")

    try:
        sources = await client.get_paginated(f"inventories/{inv_id}/inventory_sources/")
        if sources:
            log(
                f"  Found {len(sources)} inventory_sources via nested endpoint "
                f"for inventory/{inv_id}"
            )
        return cast(list[dict[str, Any]], sources)
    except Exception as exc:  # nosec B110
        log(f"  Warning: could not list inventory_sources for inventory/{inv_id}: {exc}")
        return []


def _unified_job_template_node_dep(node: dict[str, Any]) -> tuple[str | None, int | None]:
    """Return (resource_type, source_id) for a workflow node's unified job template."""
    ujt_source_id = node.get("unified_job_template")
    if ujt_source_id is None:
        return None, None

    ujt_summary = node.get("summary_fields", {}).get("unified_job_template", {})
    ujt_type = ujt_summary.get("unified_job_type")
    if ujt_type == "job":
        return "job_templates", int(ujt_source_id)
    if ujt_type == "workflow_job":
        return "workflow_job_templates", int(ujt_source_id)
    return "job_templates", int(ujt_source_id)


async def _enrich_job_template_from_source(
    client: Any,
    template: dict[str, Any],
    log: Callable[[str], None],
) -> None:
    """Fetch credentials, schedules, surveys, and notifications for a job template."""
    jt_id = template.get("id")
    if jt_id is None:
        return

    summary_creds = template.get("summary_fields", {}).get("credentials")
    if summary_creds is not None:
        template["_credentials"] = [cred["id"] for cred in summary_creds]
    else:
        try:
            credentials = await client.get_job_template_credentials(jt_id)
            template["_credentials"] = [cred["id"] for cred in credentials]
        except Exception as exc:  # nosec B110
            log(f"  Warning: could not fetch credentials for job_template/{jt_id}: {exc}")
            template["_credentials"] = []

    try:
        schedules_response = await client.get(f"job_templates/{jt_id}/schedules/")
        schedules = schedules_response.get("results", [])
        if schedules:
            template["schedules"] = schedules
    except Exception as exc:  # nosec B110
        log(f"  Warning: could not fetch schedules for job_template/{jt_id}: {exc}")

    try:
        survey_spec = await client.get(f"job_templates/{jt_id}/survey_spec/")
        if survey_spec and survey_spec.get("spec"):
            template["survey_spec"] = survey_spec
    except Exception as exc:  # nosec B110
        if "404" not in str(exc).lower():
            log(f"  Warning: could not fetch survey for job_template/{jt_id}: {exc}")

    notifications: dict[str, list[int]] = {}
    for notif_type in ("started", "success", "error"):
        try:
            notif_response = await client.get(
                f"job_templates/{jt_id}/notification_templates_{notif_type}/"
            )
            notif_templates = notif_response.get("results", [])
            if notif_templates:
                notifications[f"notification_templates_{notif_type}"] = [
                    nt["id"] for nt in notif_templates
                ]
        except Exception:  # nosec B110
            pass
    if notifications:
        template["notifications"] = notifications


async def _enrich_workflow_from_source(
    client: Any,
    workflow: dict[str, Any],
    log: Callable[[str], None],
) -> None:
    """Fetch nodes, schedules, surveys, and notifications for a workflow job template."""
    wf_id = workflow.get("id")
    if wf_id is None:
        return

    try:
        nodes = await client.get_workflow_nodes(wf_id)
        workflow["nodes"] = nodes
    except Exception as exc:  # nosec B110
        log(f"  Warning: could not fetch workflow nodes for workflow_job_templates/{wf_id}: {exc}")
        workflow["nodes"] = []

    try:
        survey_spec = await client.get(f"workflow_job_templates/{wf_id}/survey_spec/")
        if survey_spec and survey_spec.get("spec"):
            workflow["survey_spec"] = survey_spec
    except Exception as exc:  # nosec B110
        if "404" not in str(exc).lower():
            log(f"  Warning: could not fetch survey for workflow_job_templates/{wf_id}: {exc}")

    try:
        schedules_response = await client.get(f"workflow_job_templates/{wf_id}/schedules/")
        schedules = schedules_response.get("results", [])
        if schedules:
            workflow["schedules"] = schedules
    except Exception as exc:  # nosec B110
        log(f"  Warning: could not fetch schedules for workflow_job_templates/{wf_id}: {exc}")

    notifications: dict[str, list[int]] = {}
    for notif_type in ("started", "success", "error", "approvals"):
        try:
            notif_response = await client.get(
                f"workflow_job_templates/{wf_id}/notification_templates_{notif_type}/"
            )
            notif_templates = notif_response.get("results", [])
            if notif_templates:
                notifications[f"notification_templates_{notif_type}"] = [
                    nt["id"] for nt in notif_templates
                ]
        except Exception:  # nosec B110
            pass
    if notifications:
        workflow["notifications"] = notifications


def _classify_import_no_result(
    state: Any,
    resource_type: str,
    source_id: int,
    res_name: str,
    log: Callable[[str], None],
    detail_hint: str | None = None,
) -> tuple[str, str]:
    """Classify import_resource(None) as failed vs skipped and return log detail."""
    error_msg = detail_hint or state.get_error_message(resource_type, source_id)
    status = state.get_status(resource_type, source_id)
    if status == "failed" or error_msg:
        detail = error_msg or "import failed"
        log(f"  Failed {resource_type}/{source_id} ({res_name}): {detail}")
        return "failed", detail
    log(
        f"  Skipping {resource_type}/{source_id} ({res_name}): "
        "already recorded as migrated in state database"
    )
    return "skipped", "Already migrated in state database"


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
        await _enrich_job_template_from_source(client, jt, log)
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

    for inv_id in list(deps.get("inventories", set())):
        sources = await _list_inventory_sources_for_inventory(client, inv_id, log)
        for src in sources:
            src_id = src.get("id")
            if src_id is not None:
                _add("inventory_sources", src_id)
            _add("inventories", _fk_id(src, "inventory"))
            _add("projects", _fk_id(src, "source_project"))
            _add("credentials", _fk_id(src, "credential"))
            _add("execution_environments", _fk_id(src, "execution_environment"))
            _add("organizations", _fk_id(src, "organization"))

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


async def _resolve_workflow_dependencies(
    client: Any,
    wf_ids: list[int],
    log: Callable[[str], None],
) -> tuple[dict[str, set[int]], list[dict[str, Any]]]:
    """Walk selected workflows (and nested workflow refs) and collect dependencies."""
    deps: dict[str, set[int]] = {}

    def _add(rtype: str, rid: int | None) -> None:
        if rid is not None:
            deps.setdefault(rtype, set()).add(rid)

    wf_data_list: list[dict[str, Any]] = []
    seen_wf: set[int] = set()
    pending = list(wf_ids)

    while pending:
        wf_id = pending.pop(0)
        if wf_id in seen_wf:
            continue
        seen_wf.add(wf_id)

        try:
            wf = await client.get_resource_by_id("workflow_job_templates", wf_id)
        except Exception as exc:
            log(f"  Warning: could not fetch workflow_job_templates/{wf_id}: {exc}")
            continue

        await _enrich_workflow_from_source(client, wf, log)
        wf_data_list.append(wf)

        sf = wf.get("summary_fields", {})
        _add("organizations", wf.get("organization") or sf.get("organization", {}).get("id"))
        _add("inventories", wf.get("inventory") or sf.get("inventory", {}).get("id"))
        _add("credentials", wf.get("webhook_credential"))

        for label in sf.get("labels", {}).get("results", []):
            _add("labels", label.get("id"))

        for node in wf.get("nodes", []):
            node_rtype, node_rid = _unified_job_template_node_dep(node)
            if node_rtype and node_rid is not None:
                if node_rtype == "workflow_job_templates" and node_rid not in seen_wf:
                    pending.insert(0, node_rid)
                _add(node_rtype, node_rid)

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

    for inv_id in list(deps.get("inventories", set())):
        sources = await _list_inventory_sources_for_inventory(client, inv_id, log)
        for src in sources:
            src_id = src.get("id")
            if src_id is not None:
                _add("inventory_sources", src_id)
            _add("inventories", _fk_id(src, "inventory"))
            _add("projects", _fk_id(src, "source_project"))
            _add("credentials", _fk_id(src, "credential"))
            _add("execution_environments", _fk_id(src, "execution_environment"))
            _add("organizations", _fk_id(src, "organization"))

    jt_ids = list(deps.get("job_templates", set()))
    for jt_id in jt_ids:
        try:
            jt = await client.get_resource_by_id("job_templates", jt_id)
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
        except Exception as exc:  # nosec B110
            log(f"  Warning: could not fetch job_template/{jt_id}: {exc}")

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

    deps.pop("workflow_job_templates", None)

    total = sum(len(v) for v in deps.values())
    log(f"  Resolved {total} workflow-related dependency objects across {len(deps)} resource types")
    return deps, wf_data_list


@router.post("/selective-migrate", response_model=JobStartResponse)
async def selective_migrate(
    body: SelectiveMigrateRequest,
    db: Session = Depends(get_db),
) -> JobStartResponse:
    """Migrate selected job templates and/or workflows and their transitive dependencies."""
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
    wf_ids = body.workflow_job_template_ids
    force_update = body.force_update
    name_prefix = (body.name_prefix or "").strip()
    db_url = get_db_url()

    svc = get_job_service()

    async def _do_selective(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        import time
        from typing import cast

        from aap_migration.client.aap_source_client import AAPSourceClient
        from aap_migration.client.aap_target_client import AAPTargetClient
        from aap_migration.config import MigrationConfig, StateConfig
        from aap_migration.migration.importer import (
            InventorySourceImporter,
            JobTemplateImporter,
            WorkflowImporter,
            create_importer,
        )
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

        if name_prefix:
            log(f"Applying name prefix: '{name_prefix}'")

        async with src_client, target_client:
            if jt_ids:
                log(f"Resolving dependencies for {len(jt_ids)} job template(s)...")
                jt_deps, jt_data = await _resolve_jt_dependencies(src_client, jt_ids, log)
            else:
                jt_deps, jt_data = {}, []

            if wf_ids:
                log(f"Resolving dependencies for {len(wf_ids)} workflow(s)...")
                wf_deps, wf_data = await _resolve_workflow_dependencies(src_client, wf_ids, log)
            else:
                wf_deps, wf_data = {}, []

            deps: dict[str, set[int]] = {}
            for dep_map in (jt_deps, wf_deps):
                for rtype, ids in dep_map.items():
                    deps.setdefault(rtype, set()).update(ids)

            migration_order = [
                rt
                for rt in get_migration_order()
                if rt in deps and RESOURCE_REGISTRY[rt].has_importer
            ]
            if jt_data or deps.get("job_templates"):
                migration_order.append("job_templates")
            if wf_data:
                migration_order.append("workflow_job_templates")
            num_phases = len(migration_order)

            if force_update:
                from aap_migration.migration.models import IDMapping, MigrationProgress

                all_force_types = list(deps.keys())
                if jt_data or deps.get("job_templates"):
                    all_force_types.append("job_templates")
                if wf_data:
                    all_force_types.append("workflow_job_templates")
                cleared_progress = 0
                reset_mappings = 0
                try:
                    with state._lock:
                        from aap_migration.migration.database import get_session

                        with get_session(state.database_url) as session:
                            for rt in all_force_types:
                                if rt == "job_templates":
                                    jt_source_ids: list[int] = list(
                                        deps.get("job_templates", set())
                                    )
                                    for jt in jt_data:
                                        jt_id = jt.get("id")
                                        if jt_id is not None:
                                            jt_source_ids.append(int(jt_id))
                                    source_id_list = list(set(jt_source_ids))
                                elif rt == "workflow_job_templates":
                                    source_id_list = [
                                        int(wf["id"]) for wf in wf_data if wf.get("id") is not None
                                    ]
                                else:
                                    source_id_list = list(deps.get(rt, set()))
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
                    resources_to_import = list(jt_data)
                    selected_jt_ids = {jt.get("id") for jt in jt_data if jt.get("id") is not None}
                    for sid in deps.get("job_templates", set()):
                        if sid in selected_jt_ids:
                            continue
                        try:
                            jt = await src_client.get_resource_by_id("job_templates", sid)
                            await _enrich_job_template_from_source(src_client, jt, log)
                            resources_to_import.append(jt)
                        except Exception as exc:
                            log(f"  Warning: failed to fetch job_template/{sid}: {exc}")
                            failed += 1
                elif rtype == "workflow_job_templates":
                    resources_to_import = wf_data
                else:
                    source_ids = deps[rtype]
                    resources_to_import = []
                    for sid in source_ids:
                        try:
                            res = await src_client.get_resource_by_id(rtype, sid)
                            if rtype == "inventory_sources":
                                try:
                                    sched_resp = await src_client.get(
                                        f"inventory_sources/{sid}/schedules/"
                                    )
                                    schedules = sched_resp.get("results", [])
                                    if schedules:
                                        res["schedules"] = schedules
                                except Exception as exc:  # nosec B110
                                    log(
                                        "  Warning: could not fetch schedules for "
                                        f"inventory_sources/{sid}: {exc}"
                                    )
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

                if rtype == "inventory_sources":
                    batch: list[dict[str, Any]] = []
                    for resource in resources_to_import:
                        source_id = resource.get("id")
                        if source_id is None:
                            continue

                        if await _should_skip_migrated_resource(
                            state, target_client, rtype, int(source_id), log
                        ):
                            skipped += 1
                            res_name = resource.get("name", str(source_id))
                            log(
                                f"  Skipping {rtype}/{source_id} ({res_name}): "
                                "already exists on target"
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

                        schedules = resource.get("schedules")
                        try:
                            if transformer:
                                resource = transformer.transform_resource(
                                    resource_type=rtype,
                                    data=resource,
                                    validate=True,
                                )
                            if schedules:
                                resource["schedules"] = schedules
                            _maybe_apply_name_prefix(rtype, resource, name_prefix)
                            # transform strips read-only "id"; importer needs _source_id
                            resource["_source_id"] = int(source_id)
                            batch.append(resource)
                            exported += 1
                        except SkipResourceError as exc:
                            skipped += 1
                            res_name = resource.get("name", str(source_id))
                            log(f"  Skipping {rtype}/{source_id} ({res_name}): {exc}")
                        except Exception as exc:
                            failed += 1
                            res_name = resource.get("name", str(source_id))
                            log(f"  Transform failed {rtype}/{source_id} ({res_name}): {exc}")
                            emit(
                                {
                                    "_event": "resource_result",
                                    "phase_num": phase_num,
                                    "name": res_name,
                                    "resource_type": rtype,
                                    "result": "failed",
                                    "detail": str(exc)[:200],
                                }
                            )

                    if batch:
                        inv_src_importer = cast(
                            InventorySourceImporter,
                            create_importer(
                                resource_type="inventory_sources",
                                client=target_client,
                                state=state,
                                performance_config=migration_config.performance,
                                resource_mappings=migration_config.resource_mappings,
                                name_prefix=name_prefix,
                            ),
                        )
                        try:
                            results = await inv_src_importer.import_inventory_sources(batch)
                            created += len(results)
                            for inv_result in results:
                                emit(
                                    {
                                        "_event": "resource_result",
                                        "phase_num": phase_num,
                                        "name": inv_result.get("name", "unknown"),
                                        "resource_type": rtype,
                                        "result": "created",
                                        "detail": "",
                                    }
                                )
                            batch_failed = len(batch) - len(results)
                            if batch_failed:
                                failed += batch_failed
                                for err in inv_src_importer.import_errors:
                                    err_name = err.get("name", "unknown")
                                    err_sid = err.get("source_id", "?")
                                    err_detail = str(err.get("error", "import failed"))[:200]
                                    log(
                                        f"  Failed inventory_sources/{err_sid} "
                                        f"({err_name}): {err_detail}"
                                    )
                                    emit(
                                        {
                                            "_event": "resource_result",
                                            "phase_num": phase_num,
                                            "name": err_name,
                                            "resource_type": rtype,
                                            "result": "failed",
                                            "detail": err_detail,
                                        }
                                    )
                        except Exception as exc:
                            failed += len(batch)
                            log(f"  Error importing inventory_sources batch: {exc}")

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
                    continue

                if rtype in ("job_templates", "workflow_job_templates"):
                    template_batch: list[dict[str, Any]] = []
                    for resource in resources_to_import:
                        source_id = resource.get("id")
                        if source_id is None:
                            log(f"  Warning: skipping {rtype} record with no source id")
                            continue

                        res_name = resource.get("name", str(source_id))
                        if await _should_skip_migrated_resource(
                            state, target_client, rtype, int(source_id), log
                        ):
                            skipped += 1
                            log(
                                f"  Skipping {rtype}/{source_id} ({res_name}): "
                                "already exists on target"
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

                        log(f"  Importing {rtype}/{source_id} ({res_name})...")
                        try:
                            if transformer:
                                resource = transformer.transform_resource(
                                    resource_type=rtype,
                                    data=resource,
                                    validate=True,
                                )
                            _maybe_apply_name_prefix(rtype, resource, name_prefix)
                            resource["_source_id"] = int(source_id)
                            template_batch.append(resource)
                            exported += 1
                        except SkipResourceError as exc:
                            skipped += 1
                            log(f"  Skipping {rtype}/{source_id} ({res_name}): {exc}")
                            emit(
                                {
                                    "_event": "resource_result",
                                    "phase_num": phase_num,
                                    "name": res_name,
                                    "resource_type": rtype,
                                    "result": "skipped",
                                    "detail": str(exc)[:200],
                                }
                            )
                        except Exception as exc:
                            failed += 1
                            log(f"  Transform failed {rtype}/{source_id} ({res_name}): {exc}")
                            emit(
                                {
                                    "_event": "resource_result",
                                    "phase_num": phase_num,
                                    "name": res_name,
                                    "resource_type": rtype,
                                    "result": "failed",
                                    "detail": str(exc)[:200],
                                }
                            )

                    if template_batch:
                        if rtype == "job_templates":
                            jt_importer = cast(
                                JobTemplateImporter,
                                create_importer(
                                    resource_type="job_templates",
                                    client=target_client,
                                    state=state,
                                    performance_config=migration_config.performance,
                                    resource_mappings=migration_config.resource_mappings,
                                    name_prefix=name_prefix,
                                ),
                            )
                            results = await jt_importer.import_job_templates(template_batch)
                            importer_errors = jt_importer.import_errors
                        else:
                            wf_importer = cast(
                                WorkflowImporter,
                                create_importer(
                                    resource_type="workflow_job_templates",
                                    client=target_client,
                                    state=state,
                                    performance_config=migration_config.performance,
                                    resource_mappings=migration_config.resource_mappings,
                                    name_prefix=name_prefix,
                                ),
                            )
                            results = await wf_importer.import_workflows(template_batch)
                            importer_errors = wf_importer.import_errors

                        for item in results:
                            item_name = item.get("name", "unknown")
                            if item.get("_skipped") or item.get("_already_migrated"):
                                skipped += 1
                                detail = item.get("_skip_reason", "Already migrated")
                                result_action = "skipped"
                            else:
                                created += 1
                                detail = ""
                                result_action = "created"
                                log(f"  Created {rtype}/{item.get('id')} ({item_name})")
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

                        template_batch_failed = len(template_batch) - len(results)
                        batch_errors = list(importer_errors)
                        if batch_errors:
                            failed += len(batch_errors)
                            for err in batch_errors:
                                err_name = err.get("name", "unknown")
                                err_sid = err.get("source_id", "?")
                                err_detail = str(err.get("error", "import failed"))
                                log(f"  Failed {rtype}/{err_sid} ({err_name}): {err_detail}")
                                emit(
                                    {
                                        "_event": "resource_result",
                                        "phase_num": phase_num,
                                        "name": err_name,
                                        "resource_type": rtype,
                                        "result": "failed",
                                        "detail": err_detail[:200],
                                    }
                                )
                        elif template_batch_failed:
                            failed += template_batch_failed
                            for item in template_batch:
                                raw_sid = item.get("_source_id")
                                if raw_sid is None:
                                    continue
                                sid = int(raw_sid)
                                res_name = item.get("name", str(sid))
                                result_action, detail = _classify_import_no_result(
                                    state, rtype, sid, res_name, log
                                )
                                if result_action == "failed":
                                    emit(
                                        {
                                            "_event": "resource_result",
                                            "phase_num": phase_num,
                                            "name": res_name,
                                            "resource_type": rtype,
                                            "result": "failed",
                                            "detail": detail[:200],
                                        }
                                    )
                    else:
                        log(f"  No {rtype} to import in this phase")

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
                    continue

                importer = create_importer(
                    resource_type=rtype,
                    client=target_client,
                    state=state,
                    performance_config=migration_config.performance,
                    resource_mappings=migration_config.resource_mappings,
                    name_prefix=name_prefix,
                )

                for resource in resources_to_import:
                    source_id = resource.get("id")
                    if source_id is None:
                        continue

                    if await _should_skip_migrated_resource(
                        state, target_client, rtype, int(source_id), log
                    ):
                        skipped += 1
                        res_name = resource.get("name", resource.get("username", str(source_id)))
                        log(
                            f"  Skipping {rtype}/{source_id} ({res_name}): already exists on target"
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
                        except Exception as exc:
                            failed += 1
                            res_name = resource.get(
                                "name", resource.get("username", str(source_id))
                            )
                            log(f"  Transform failed {rtype}/{source_id} ({res_name}): {exc}")
                            emit(
                                {
                                    "_event": "resource_result",
                                    "phase_num": phase_num,
                                    "name": res_name,
                                    "resource_type": rtype,
                                    "result": "failed",
                                    "detail": str(exc)[:200],
                                }
                            )
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

                    _maybe_apply_name_prefix(rtype, resource, name_prefix)

                    exported += 1
                    res_name = resource.get("name", resource.get("username", str(source_id)))
                    log(f"  Importing {rtype}/{source_id} ({res_name})...")

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
                            if result.get("_skipped") or result.get("_already_migrated"):
                                skipped += 1
                                result_action = "skipped"
                                detail = result.get("_skip_reason", "Duplicate exists in target")
                            else:
                                created += 1
                                result_action = "created"
                                detail = ""
                                log(f"  Created {rtype}/{target_id} ({res_name})")
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
                            detail_hint = importer._failure_detail_for_resource(
                                rtype, int(source_id)
                            )
                            result_action, detail = _classify_import_no_result(
                                state,
                                rtype,
                                int(source_id),
                                res_name,
                                log,
                                detail_hint=detail_hint,
                            )
                            if result_action == "failed":
                                failed += 1
                            else:
                                skipped += 1
                            emit(
                                {
                                    "_event": "resource_result",
                                    "phase_num": phase_num,
                                    "name": res_name,
                                    "resource_type": rtype,
                                    "result": result_action,
                                    "detail": detail[:200],
                                }
                            )
                    except Exception as exc:
                        failed += 1
                        fail_name = resource.get(
                            "name",
                            resource.get("username", str(source_id)),
                        )
                        log(f"  Failed {rtype}/{source_id} ({fail_name}): {exc}")
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

    selection_parts: list[str] = []
    if jt_ids:
        selection_parts.append(f"{len(jt_ids)} JT(s)")
    if wf_ids:
        selection_parts.append(f"{len(wf_ids)} workflow(s)")
    job_id = svc.start_job(
        f"Selective migrate {' + '.join(selection_parts)} → {dest_conn.name}",
        "selective-migration",
        _do_selective,
    )
    return JobStartResponse(job_id=job_id)
