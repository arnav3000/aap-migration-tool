"""Migration orchestration helpers for API and CLI paths.

Shared export → transform → import orchestration used by planner router,
migration router, and operations selective ETL. API paths run in-memory
by design (no exports/ or xformed/ disk writes); see pipeline module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any, cast

from aap_migration.api.services.job_service import Job, JobStatus
from aap_migration.resources import (
    excluded_preview_count,
    is_host_inventory_membership_excluded,
    is_resource_type_fully_excluded,
)

logger = logging.getLogger(__name__)

_CRED_CONSUMERS = [
    ("projects", "credential", True),
    ("execution_environments", "credential", True),
    ("inventory_sources", "credential", True),
    ("credential_input_sources", "source_credential", True),
    ("instance_groups", "credential", False),
]


async def _build_credential_review(
    src_client: Any,
    created_creds: list[dict[str, str]],
    org_ids: list[int],
) -> list[dict[str, Any]]:
    """Query source AAP to find which created credentials are actually used."""

    cred_source_ids = {c["source_id"] for c in created_creds}
    used_by: dict[str, list[dict[str, str]]] = {sid: [] for sid in cred_source_ids}

    async def _query_consumer(resource_type: str, field_name: str, filter_org: bool) -> None:
        try:
            resp = await src_client.get(f"{resource_type}/", params={"page_size": 200})
            for item in resp.get("results", []):
                cred_ref = item.get(field_name)
                if cred_ref is None or str(cred_ref) not in cred_source_ids:
                    continue
                if filter_org and org_ids:
                    item_org = item.get("organization") or (
                        item.get("summary_fields", {}).get("organization", {}).get("id")
                    )
                    if item_org and item_org not in org_ids:
                        continue
                used_by[str(cred_ref)].append(
                    {
                        "resource_type": resource_type,
                        "resource_name": item.get("name", str(item.get("id", "?"))),
                    }
                )
        except Exception:  # nosec B110
            pass

    async def _query_galaxy(org_id: int) -> None:
        try:
            resp = await src_client.get(f"organizations/{org_id}/galaxy_credentials/")
            for gc in resp.get("results", []):
                gc_id = str(gc.get("id", ""))
                if gc_id in cred_source_ids:
                    used_by[gc_id].append(
                        {
                            "resource_type": "organizations (galaxy)",
                            "resource_name": f"Org {org_id}",
                        }
                    )
        except Exception:  # nosec B110
            pass

    await asyncio.gather(
        *[_query_consumer(rt, fn, fo) for rt, fn, fo in _CRED_CONSUMERS],
        *[_query_galaxy(oid) for oid in org_ids],
    )

    result: list[dict[str, Any]] = []
    for cred in created_creds:
        sid = cred["source_id"]
        result.append(
            {
                "name": cred["name"],
                "credential_type": cred["credential_type"],
                "organization": cred["organization"],
                "source": cred.get("source", ""),
                "name_prefix": cred.get("name_prefix", ""),
                "used_by": used_by.get(sid, []),
            }
        )
    result.sort(
        key=lambda c: (len(c["used_by"]) == 0, c["source"], c["credential_type"], c["name"])
    )
    return result


# ---------------------------------------------------------------------------
# Phase execution helpers
# ---------------------------------------------------------------------------


def _build_source_contexts(
    source_configs: list[dict[str, Any]],
    dest_cfg: Any,
    dest_auth_scheme: str,
    db_url: str,
) -> tuple[Any, Any, list[dict[str, Any]]]:
    """Build the target client and per-source context dicts."""
    from aap_migration.client.aap_source_client import AAPSourceClient
    from aap_migration.client.aap_target_client import AAPTargetClient
    from aap_migration.config import AAPInstanceConfig, MigrationConfig, StateConfig
    from aap_migration.migration.state import MigrationState

    target_config = AAPInstanceConfig(
        url=dest_cfg.url,
        token=dest_cfg.token,
        verify_ssl=dest_cfg.verify_ssl,
        timeout=dest_cfg.timeout,
    )
    target_client = AAPTargetClient(target_config, auth_scheme=dest_auth_scheme)

    sources: list[dict[str, Any]] = []
    for src_cfg in source_configs:
        src_config = AAPInstanceConfig(
            url=src_cfg["url"],
            token=src_cfg["token"],
            verify_ssl=src_cfg["verify_ssl"],
            timeout=src_cfg["timeout"],
        )
        migration_config = MigrationConfig(
            source=src_config,
            target=target_config,
            state=StateConfig(db_path=db_url),
        )
        sources.append(
            {
                "src_config": src_config,
                "migration_config": migration_config,
                "src_client": AAPSourceClient(
                    src_config,
                    auth_scheme=src_cfg.get("auth_scheme", "Bearer"),
                ),
                "state": MigrationState(
                    migration_config.state,
                    source_key=str(src_cfg.get("source_key") or src_cfg.get("connection_id") or ""),
                ),
                "name_prefix": src_cfg.get("name_prefix", ""),
                "connection_name": src_cfg.get("connection_name", "") or src_cfg["url"],
                "org_ids": src_cfg["org_ids"],
                "url": src_cfg["url"],
            }
        )
    return target_config, target_client, sources


def _resource_display_name(resource: dict[str, Any], source_id: Any) -> str:
    return str(resource.get("name") or resource.get("username") or source_id)


def _import_result_detail(result: Any) -> str:
    """Human-readable reason from importer marker fields."""
    if not isinstance(result, dict):
        return ""
    reason = result.get("_skip_reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    if result.get("_already_migrated"):
        return "Already migrated in state — update secrets if needed"
    if result.get("_skipped"):
        return "Matched existing managed resource on target — mapped only"
    return ""


def _emit_resource_result(
    emit: Callable[[dict[str, Any]], None],
    log: Callable[[str], None],
    *,
    phase_num: int,
    name: str,
    rtype: str,
    result: str,
    detail: str = "",
) -> None:
    """Emit a resource_result event and a plain-text log line for skips/fails."""
    detail = (detail or "")[:300]
    emit(
        {
            "_event": "resource_result",
            "phase_num": phase_num,
            "name": name,
            "resource_type": rtype,
            "result": result,
            "detail": detail,
        }
    )
    if result in ("skipped", "exists", "failed") and detail:
        log(f"  {result.capitalize()} {rtype}/{name}: {detail}")
    elif result in ("skipped", "exists", "failed"):
        log(f"  {result.capitalize()} {rtype}/{name}")


async def _migrate_resource_type(
    rtype: str,
    sources: list[dict[str, Any]],
    target_client: Any,
    phase_num: int,
    emit: Callable[[dict[str, Any]], None],
    log: Callable[[str], None],
    created_creds: list[dict[str, str]],
) -> tuple[int, int, int, int]:
    """Export → filter → transform → import one resource type across all sources.

    Returns (created, skipped, failed, exported).
    """

    from aap_migration.migration.exporter import create_exporter
    from aap_migration.migration.importer import create_importer
    from aap_migration.migration.transformer import SkipResourceError, create_transformer
    from aap_migration.resources import RESOURCE_REGISTRY

    info = RESOURCE_REGISTRY[rtype]
    phase_start = time.monotonic()
    created = 0
    skipped = 0
    failed = 0
    exported = 0
    last_progress = time.monotonic()
    PROGRESS_INTERVAL = 2.0

    # Fast-path: every source fully excluded this type — skip without exporting.
    if sources and all(
        is_resource_type_fully_excluded(
            rtype, src.get("excluded_ids"), src.get("preview_resources")
        )
        for src in sources
    ):
        for src in sources:
            skipped += excluded_preview_count(
                rtype, src.get("excluded_ids"), src.get("preview_resources")
            )
        log(f"Skipping {info.description}: all {skipped} resource(s) excluded by user")
        emit(
            {
                "_event": "phase_complete",
                "phase_num": phase_num,
                "description": info.description,
                "created": 0,
                "updated": 0,
                "skipped": skipped,
                "failed": 0,
                "exported": 0,
                "duration": "0.0s",
                "warnings": {},
            }
        )
        return 0, skipped, 0, 0

    # Credentials depend on built-in credential types that may never be
    # "migrated". Map them by name onto the target before export/transform.
    if rtype in ("credentials", "credential_types"):
        from aap_migration.migration.credential_type_utils import map_managed_credential_types

        for src in sources:
            try:
                mapped = await map_managed_credential_types(
                    src["src_client"], target_client, src["state"]
                )
                if mapped:
                    log(
                        f"  Mapped {mapped} managed credential type(s) "
                        f"for {src.get('connection_name') or src['url']}"
                    )
            except Exception as exc:
                log(f"  Warning: could not map managed credential types: {exc}")

    for src in sources:
        src_client = src["src_client"]
        state = src["state"]
        migration_config = src["migration_config"]
        name_prefix: str = src["name_prefix"]
        connection_name: str = src.get("connection_name") or src["url"]
        org_ids: list[int] = src["org_ids"]
        bulk_skipped_excluded = 0
        bulk_skipped_org = 0
        bulk_skipped_host_cascade = 0

        # Per-source fast-path when preview proves full exclusion.
        if is_resource_type_fully_excluded(
            rtype, src.get("excluded_ids"), src.get("preview_resources")
        ):
            n = excluded_preview_count(rtype, src.get("excluded_ids"), src.get("preview_resources"))
            skipped += n
            log(
                f"  Skipping {info.description} from {connection_name}: "
                f"all {n} resource(s) excluded by user"
            )
            continue

        try:
            from aap_migration.migration.target_bootstrap import bootstrap_mappings_for_type

            bootstrap = await bootstrap_mappings_for_type(
                rtype,
                src_client,
                target_client,
                state,
                name_prefix=name_prefix,
                org_ids=org_ids or None,
            )
            if bootstrap.mapped:
                log(
                    f"  Bootstrapped {bootstrap.mapped} existing {info.description} "
                    f"from target ({bootstrap.unmatched} not on target)"
                )

            exporter = create_exporter(
                resource_type=rtype,
                client=src_client,
                state=state,
                performance_config=migration_config.performance,
            )
            transformer = (
                create_transformer(
                    resource_type=rtype, dry_run=False, state=state, defer_project_sync=False
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
                name_prefix=name_prefix,
            )

            excluded_for_type = {str(x) for x in ((src.get("excluded_ids") or {}).get(rtype) or [])}

            async for resource in exporter.export():
                source_id = resource.get("id")
                if source_id is None:
                    if rtype == "host_inventory_memberships":
                        source_id = f"{resource.get('host_id')}_{resource.get('inventory_id')}"
                    elif rtype == "settings":
                        source_id = "settings"
                    else:
                        continue

                if org_ids and not _resource_in_orgs(rtype, resource, source_id, org_ids):
                    skipped += 1
                    bulk_skipped_org += 1
                    continue

                if excluded_for_type and str(source_id) in excluded_for_type:
                    skipped += 1
                    bulk_skipped_excluded += 1
                    continue

                if rtype == "host_inventory_memberships" and is_host_inventory_membership_excluded(
                    resource, src.get("excluded_ids")
                ):
                    skipped += 1
                    bulk_skipped_host_cascade += 1
                    continue

                raw_summary = resource.get("summary_fields", {})
                res_name = _resource_display_name(resource, source_id)

                if transformer:
                    try:
                        resource = transformer.transform_resource(
                            resource_type=rtype, data=resource, validate=True
                        )
                        res_name = _resource_display_name(resource, source_id)
                    except SkipResourceError as skip_exc:
                        skipped += 1
                        _emit_resource_result(
                            emit,
                            log,
                            phase_num=phase_num,
                            name=res_name,
                            rtype=rtype,
                            result="skipped",
                            detail=str(skip_exc),
                        )
                        continue
                    except Exception as exc:
                        failed += 1
                        _emit_resource_result(
                            emit,
                            log,
                            phase_num=phase_num,
                            name=res_name,
                            rtype=rtype,
                            result="failed",
                            detail=f"Transform error: {exc}",
                        )
                        continue

                if name_prefix:
                    from aap_migration.utils.naming import apply_name_prefix

                    apply_name_prefix(rtype, resource, name_prefix)
                    res_name = _resource_display_name(resource, source_id)

                exported += 1

                try:
                    if rtype == "host_inventory_memberships":
                        result = await cast(Any, importer).import_resource(resource=resource)
                    else:
                        result = await importer.import_resource(
                            resource_type=rtype,
                            source_id=int(source_id),
                            data=resource,
                        )
                    res_name = _resource_display_name(resource, source_id)
                    import_err = None
                    outcome = "created"
                    detail = ""

                    if result:
                        already_present = isinstance(result, dict) and bool(
                            result.get("_already_migrated") or result.get("_skipped")
                        )
                        if already_present:
                            outcome = "exists"
                            detail = _import_result_detail(result)
                            skipped += 1
                            _emit_resource_result(
                                emit,
                                log,
                                phase_num=phase_num,
                                name=res_name,
                                rtype=rtype,
                                result=outcome,
                                detail=detail,
                            )
                        else:
                            created += 1
                            emit(
                                {
                                    "_event": "resource_result",
                                    "phase_num": phase_num,
                                    "name": res_name,
                                    "resource_type": rtype,
                                    "result": "created",
                                    "detail": "",
                                }
                            )
                    else:
                        # Importers often return None for both skips and handled failures.
                        # Prefer import_errors / state so the UI shows the real reason.
                        for err in getattr(importer, "import_errors", []) or []:
                            if err.get("source_id") == int(source_id):
                                import_err = err
                        if import_err:
                            outcome = "failed"
                            detail = import_err.get("error") or "Import failed"
                            if import_err.get("error_type"):
                                detail = f"{import_err['error_type']}: {detail}"
                            failed += 1
                            _emit_resource_result(
                                emit,
                                log,
                                phase_num=phase_num,
                                name=res_name,
                                rtype=rtype,
                                result=outcome,
                                detail=detail,
                            )
                        elif state.is_migrated(rtype, int(source_id)):
                            outcome = "exists"
                            detail = "Already migrated in state — update secrets if needed"
                            skipped += 1
                            _emit_resource_result(
                                emit,
                                log,
                                phase_num=phase_num,
                                name=res_name,
                                rtype=rtype,
                                result=outcome,
                                detail=detail,
                            )
                        else:
                            outcome = "skipped"
                            detail = (
                                "Import returned no result (already migrated, "
                                "filtered, or failed — check server logs)"
                            )
                            skipped += 1
                            _emit_resource_result(
                                emit,
                                log,
                                phase_num=phase_num,
                                name=res_name,
                                rtype=rtype,
                                result=outcome,
                                detail=detail,
                            )

                    # Track every credential we touched for the post-cred secret pause,
                    # including already-migrated ones — secrets are never exported and
                    # must still be filled in before dependent resources run.
                    if rtype == "credentials" and outcome != "failed":
                        created_creds.append(
                            {
                                "name": res_name,
                                "credential_type": raw_summary.get("credential_type", {}).get(
                                    "name", "Unknown"
                                ),
                                "organization": raw_summary.get("organization", {}).get("name", ""),
                                "source_id": str(source_id),
                                "source": connection_name,
                                "name_prefix": name_prefix,
                            }
                        )
                except Exception as exc:
                    failed += 1
                    _emit_resource_result(
                        emit,
                        log,
                        phase_num=phase_num,
                        name=_resource_display_name(resource, source_id),
                        rtype=rtype,
                        result="failed",
                        detail=str(exc),
                    )

                now = time.monotonic()
                if now - last_progress >= PROGRESS_INTERVAL:
                    emit(
                        {
                            "_event": "phase_progress",
                            "phase_num": phase_num,
                            "exported": exported,
                            "created": created,
                            "skipped": skipped,
                            "failed": failed,
                            "rate": f"{exported / max(now - phase_start, 0.1):.0f}/s",
                            "elapsed": f"{now - phase_start:.1f}s",
                        }
                    )
                    last_progress = now

            if bulk_skipped_excluded:
                _emit_resource_result(
                    emit,
                    log,
                    phase_num=phase_num,
                    name=f"({bulk_skipped_excluded} resources)",
                    rtype=rtype,
                    result="skipped",
                    detail="Excluded by user",
                )
            if bulk_skipped_org:
                _emit_resource_result(
                    emit,
                    log,
                    phase_num=phase_num,
                    name=f"({bulk_skipped_org} resources)",
                    rtype=rtype,
                    result="skipped",
                    detail="Not in selected organizations for this phase",
                )
            if bulk_skipped_host_cascade:
                _emit_resource_result(
                    emit,
                    log,
                    phase_num=phase_num,
                    name=f"({bulk_skipped_host_cascade} resources)",
                    rtype=rtype,
                    result="skipped",
                    detail="Host excluded by user",
                )

        except Exception as exc:
            failed += 1
            emit({"_event": "phase_error", "phase_num": phase_num, "error": str(exc)})
            log(f"  Error on {rtype} from {src['url']}: {exc}")

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

    return created, skipped, failed, exported


def _resource_in_orgs(
    rtype: str, resource: dict[str, Any], source_id: Any, org_ids: list[int]
) -> bool:
    """Check whether a resource belongs to one of the selected orgs.

    Global resource types (settings, instances, instance_groups, credential_types,
    users) are intentionally included in every phase. Org-scoped types must match
    an selected org id via ``organization`` or ``summary_fields.organization.id``.
    """
    if rtype == "organizations":
        try:
            return int(source_id) in org_ids
        except (TypeError, ValueError):
            return False
    # Truly global / infrastructure types — always in scope for a phase
    if rtype in (
        "settings",
        "instances",
        "instance_groups",
        "credential_types",
        "users",
        "system_job_templates",
    ):
        return True
    # Memberships are filtered by their inventory's org during export; allow through
    # here and let import resolve inventory mapping (missing → skip/fail with reason).
    if rtype == "host_inventory_memberships":
        return True

    res_org = resource.get("organization")
    sf_org = resource.get("summary_fields", {}).get("organization", {}).get("id")
    if res_org in org_ids or sf_org in org_ids:
        return True
    # Credentials/EEs may be user-owned or global (organization=null). Include them
    # when they have no org so they are not silently dropped from every phase.
    if rtype in ("credentials", "execution_environments", "applications") and (
        res_org is None and sf_org is None
    ):
        return True
    return False


async def _handle_credential_pause(
    job: Job,
    svc: Any,
    created_creds: list[dict[str, str]],
    sources: list[dict[str, Any]],
    plan_id: str,
    phase_id: str,
    emit: Callable[[dict[str, Any]], None],
    log: Callable[[str], None],
) -> None:
    """Pause migration for credential secret review, wait for user to resume."""
    import logging

    logger = logging.getLogger(__name__)

    if not created_creds:
        return

    # Review each source's credentials against that source only — credential
    # IDs are not comparable across AAP instances.
    review_tasks = []
    matched: set[str] = set()
    for s in sources:
        source_label = s.get("connection_name") or s["url"]
        source_creds = [c for c in created_creds if c.get("source") == source_label]
        if not source_creds:
            continue
        for c in source_creds:
            matched.add(f"{c.get('source', '')}:{c.get('source_id', '')}:{c.get('name', '')}")
        review_tasks.append(_build_credential_review(s["src_client"], source_creds, s["org_ids"]))

    reviews = await asyncio.gather(*review_tasks) if review_tasks else []
    cred_review: list[dict[str, Any]] = []
    for r in reviews:
        cred_review.extend(r)

    # Never skip the pause because of source-label mismatches — secrets still
    # need to be filled in before dependent resources migrate.
    unmatched = [
        c
        for c in created_creds
        if f"{c.get('source', '')}:{c.get('source_id', '')}:{c.get('name', '')}" not in matched
    ]
    if unmatched:
        logger.warning(
            "credential_pause_unmatched_sources count=%s sources=%s",
            len(unmatched),
            sorted({c.get("source", "") for c in unmatched}),
        )
        for c in unmatched:
            cred_review.append(
                {
                    "name": c["name"],
                    "credential_type": c.get("credential_type", ""),
                    "organization": c.get("organization", ""),
                    "source": c.get("source", ""),
                    "name_prefix": c.get("name_prefix", ""),
                    "used_by": [],
                }
            )

    if not cred_review:
        # Last-resort fallback so a review-builder failure cannot skip the pause.
        cred_review = [
            {
                "name": c["name"],
                "credential_type": c.get("credential_type", ""),
                "organization": c.get("organization", ""),
                "source": c.get("source", ""),
                "name_prefix": c.get("name_prefix", ""),
                "used_by": [],
            }
            for c in created_creds
        ]

    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for cr in cred_review:
        key = (cr.get("source", ""), cr["name"])
        if key not in seen:
            seen.add(key)
            deduped.append(cr)
    cred_review = deduped

    emit({"_event": "credential_pause", "credentials": cred_review})
    log("Paused — waiting for user to update credential secrets on the target and resume.")
    job.result = job.result or {}
    job.result["credential_review"] = cred_review
    if plan_id:
        job.result["_paused_plan_id"] = plan_id
    if phase_id:
        job.result["_paused_phase_id"] = phase_id
    job.status = JobStatus.WAITING_FOR_INPUT
    svc.persist_job(job)
    await job.wait_for_resume()
    job.status = JobStatus.RUNNING
    log("Resumed — continuing migration.")


async def _run_cac_org_update(
    sources: list[dict[str, Any]],
    target_client: Any,
    phase_num: int,
    emit: Callable[[dict[str, Any]], None],
    log: Callable[[str], None],
) -> int:
    """CaC org-update pass: PATCH orgs to assign EE, galaxy creds, etc."""

    sem = asyncio.Semaphore(5)

    async def _update_one_org(src: dict[str, Any], org_id: int) -> int:
        async with sem:
            try:
                src_client = src["src_client"]
                state = src["state"]
                org_data = await src_client.get(f"organizations/{org_id}/")
                target_org_id = state.get_mapped_id("organizations", org_id)
                if target_org_id is None:
                    return 0
                patch: dict[str, Any] = {}
                if org_data.get("default_environment"):
                    mapped_ee = state.get_mapped_id(
                        "execution_environments", org_data["default_environment"]
                    )
                    if mapped_ee:
                        patch["default_environment"] = mapped_ee
                count = 0
                if patch:
                    await target_client.update_resource("organizations", target_org_id, patch)
                    count += 1
                    emit(
                        {
                            "_event": "resource_result",
                            "phase_num": phase_num,
                            "name": org_data.get("name", str(org_id)),
                            "resource_type": "organizations",
                            "result": "updated",
                            "detail": "CaC org-update pass",
                        }
                    )

                try:
                    galaxy_resp = await src_client.get(
                        f"organizations/{org_id}/galaxy_credentials/"
                    )
                    for gc in galaxy_resp.get("results", []):
                        gc_source_id = gc.get("id")
                        if gc_source_id is None:
                            continue
                        mapped_gc = state.get_mapped_id("credentials", gc_source_id)
                        if mapped_gc:
                            await target_client.post(
                                f"organizations/{target_org_id}/galaxy_credentials/",
                                {"id": mapped_gc},
                            )
                except Exception as gc_exc:
                    log(f"  Warning: galaxy cred association for org {org_id}: {gc_exc}")
                return count
            except Exception as org_exc:
                log(f"  Warning: CaC org-update for {org_id}: {org_exc}")
                return 0

    tasks = [_update_one_org(src, oid) for src in sources for oid in src["org_ids"]]
    results = await asyncio.gather(*tasks)
    return sum(results)


def _sort_disk_resource_types(resource_types: list[str]) -> list[str]:
    from aap_migration.resources import RESOURCE_REGISTRY, normalize_resource_type

    known: list[tuple[str, int]] = []
    unknown: list[str] = []
    for rtype in resource_types:
        normalized = normalize_resource_type(rtype)
        info = RESOURCE_REGISTRY.get(normalized)
        if info and hasattr(info, "migration_order"):
            known.append((rtype, info.migration_order))
        else:
            unknown.append(rtype)
    known.sort(key=lambda item: item[1])
    return [rtype for rtype, _ in known] + unknown


def _load_export_metadata(input_dir: Any) -> dict[str, Any]:
    from pathlib import Path

    metadata_file = Path(input_dir) / "metadata.json"
    if not metadata_file.exists():
        raise FileNotFoundError(f"metadata.json not found in {input_dir}")
    with open(metadata_file) as fh:
        return cast(dict[str, Any], json.load(fh))


async def run_disk_export(
    source_client: Any,
    state: Any,
    output_dir: Any,
    *,
    resource_types: list[str] | None = None,
    records_per_file: int = 1000,
    resume: bool = False,
    performance_config: Any | None = None,
    export_config: Any | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Export resources from source AAP to disk (exports/)."""
    from datetime import UTC, datetime
    from pathlib import Path

    from aap_migration.config import ExportConfig, PerformanceConfig
    from aap_migration.migration.parallel_exporter import ParallelExportCoordinator
    from aap_migration.resources import get_exportable_types

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    types_to_export = resource_types or get_exportable_types()
    perf = performance_config or PerformanceConfig()
    export_cfg = export_config or ExportConfig()

    if log:
        log(f"Exporting {len(types_to_export)} resource type(s) to {out}")

    coordinator = ParallelExportCoordinator(
        source_client=source_client,
        migration_state=state,
        performance_config=perf,
        output_dir=out,
        records_per_file=records_per_file,
        export_config=export_cfg,
    )

    def _progress(rtype: str, stats: dict[str, Any]) -> None:
        if log and stats.get("exported", 0) % 100 == 0 and stats.get("exported", 0) > 0:
            log(f"  {rtype}: exported {stats['exported']}")

    results = await coordinator.export_all_parallel(
        types_to_export,
        resume=resume,
        progress_callback=_progress,
    )

    export_stats: dict[str, dict[str, int]] = {}
    total_resources = 0
    for rtype, stats in results.items():
        count = int(stats.get("exported", 0))
        export_stats[rtype] = {"count": count, "failed": int(stats.get("failed", 0))}
        total_resources += count
        if log:
            log(f"  {rtype}: {count} exported")

    metadata = {
        "export_timestamp": datetime.now(UTC).isoformat(),
        "source_url": getattr(getattr(source_client, "config", None), "url", ""),
        "total_resources": total_resources,
        "records_per_file": records_per_file,
        "resource_types": export_stats,
    }
    with open(out / "metadata.json", "w") as fh:
        json.dump(metadata, fh, indent=2)

    if log:
        log(f"Export complete: {total_resources} resources written to {out}")

    return {
        "output_dir": str(out),
        "total_resources": total_resources,
        "resource_types": export_stats,
    }


async def run_disk_transform(
    state: Any,
    input_dir: Any,
    output_dir: Any,
    *,
    resource_types: list[str] | None = None,
    target_client: Any | None = None,
    performance_config: Any | None = None,
    migration_config: Any | None = None,
    defer_project_sync: bool = True,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Transform RAW exports on disk into xformed/ payloads."""
    from datetime import UTC, datetime
    from pathlib import Path

    from aap_migration.config import PerformanceConfig
    from aap_migration.migration.parallel_transformer import ParallelTransformCoordinator

    in_dir = Path(input_dir)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    metadata = _load_export_metadata(in_dir)
    available = list(metadata.get("resource_types", {}).keys())
    types_to_transform = _sort_disk_resource_types(resource_types or available)
    if not types_to_transform:
        raise ValueError("No resource types found to transform")

    perf = performance_config or PerformanceConfig()

    if log:
        log(f"Transforming {len(types_to_transform)} resource type(s): {in_dir} -> {out_dir}")

    coordinator = ParallelTransformCoordinator(
        migration_state=state,
        performance_config=perf,
        input_dir=in_dir,
        output_dir=out_dir,
        target_client=target_client,
        skip_pending_deletion=True,
        config=migration_config,
        defer_project_sync=defer_project_sync,
    )

    def _progress(rtype: str, stats: dict[str, Any]) -> None:
        if log and stats.get("count", 0) % 100 == 0 and stats.get("count", 0) > 0:
            log(f"  {rtype}: transformed {stats['count']}")

    results = await coordinator.transform_all_parallel(types_to_transform, _progress)

    transform_stats: dict[str, dict[str, int]] = {}
    total_transformed = 0
    total_failed = 0
    for rtype, stats in results.items():
        count = int(stats.get("count", 0))
        failed = int(stats.get("failed", 0))
        transform_stats[rtype] = {"count": count, "failed": failed}
        total_transformed += count
        total_failed += failed
        if log:
            log(f"  {rtype}: {count} transformed, {failed} failed")

    transformed_metadata = {
        "transform_timestamp": datetime.now(UTC).isoformat(),
        "source_metadata": metadata,
        "total_resources": total_transformed,
        "total_failed": total_failed,
        "records_per_file": metadata.get("records_per_file", 1000),
        "resource_types": transform_stats,
    }
    with open(out_dir / "metadata.json", "w") as fh:
        json.dump(transformed_metadata, fh, indent=2)

    if log:
        log(
            f"Transform complete: {total_transformed} resources, {total_failed} failed -> {out_dir}"
        )

    return {
        "input_dir": str(in_dir),
        "output_dir": str(out_dir),
        "total_transformed": total_transformed,
        "total_failed": total_failed,
        "resource_types": transform_stats,
    }


async def run_disk_import(
    source_client: Any,
    target_client: Any,
    state: Any,
    input_dir: Any,
    resource_types: list[str],
    *,
    performance_config: Any | None = None,
    resource_mappings: dict[str, dict[str, str]] | None = None,
    name_prefix: str = "",
    org_ids: list[int] | None = None,
    dry_run: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Import transformed resources from xformed/ into target AAP."""
    from pathlib import Path
    from types import SimpleNamespace

    from aap_migration.config import PerformanceConfig
    from aap_migration.migration.importer import create_importer
    from aap_migration.migration.pipeline import bootstrap_resource_type, run_import_loop
    from aap_migration.resources import RESOURCE_REGISTRY

    in_dir = Path(input_dir)
    metadata = _load_export_metadata(in_dir)
    available = list(metadata.get("resource_types", {}).keys())
    ordered_types = _sort_disk_resource_types(
        [rt for rt in resource_types if rt in available] if resource_types else available
    )
    perf = performance_config or PerformanceConfig()

    totals = {"imported": 0, "skipped": 0, "failed": 0}
    per_type: dict[str, dict[str, int]] = {}

    if log:
        log(f"Importing {len(ordered_types)} resource type(s) from {in_dir}")

    for rtype in ordered_types:
        info = RESOURCE_REGISTRY.get(rtype)
        if not info or not info.has_importer:
            if log:
                log(f"  {rtype}: skipped (no importer)")
            continue

        resource_dir = in_dir / rtype
        if not resource_dir.exists():
            if log:
                log(f"  {rtype}: no directory, skipping")
            continue

        json_files = sorted(resource_dir.glob(f"{rtype}_*.json"))
        if not json_files:
            if log:
                log(f"  {rtype}: no files, skipping")
            continue

        if log:
            log(f"  {rtype}: loading {len(json_files)} file(s)...")

        resources: list[dict[str, Any]] = []
        for json_file in json_files:
            with open(json_file) as fh:
                batch = json.load(fh)
            if isinstance(batch, list):
                resources.extend(batch)

        if not resources:
            continue

        if org_ids:
            filtered: list[dict[str, Any]] = []
            for resource in resources:
                source_id = resource.get("_source_id") or resource.get("id")
                if _resource_in_orgs(rtype, resource, source_id, org_ids):
                    filtered.append(resource)
            resources = filtered

        for resource in resources:
            source_id = resource.get("_source_id") or resource.get("id")
            if source_id is not None and "_source_id" not in resource:
                resource["_source_id"] = source_id

        if not info.has_transformer and not dry_run:
            for resource in resources:
                source_id = resource.get("_source_id")
                if source_id is None:
                    continue
                if not state.has_source_mapping(rtype, int(source_id)):
                    state.create_source_mapping(
                        resource_type=rtype,
                        source_id=int(source_id),
                        source_name=resource.get("name", resource.get("username")),
                    )

        if name_prefix:
            from aap_migration.utils.naming import apply_name_prefix

            for resource in resources:
                apply_name_prefix(rtype, resource, name_prefix)

        await bootstrap_resource_type(
            rtype,
            source_client=source_client,
            target_client=target_client,
            state=state,
            name_prefix=name_prefix,
            org_ids=org_ids,
        )

        importer = create_importer(
            resource_type=rtype,
            client=target_client,
            state=state,
            performance_config=perf,
            resource_mappings=resource_mappings,
            name_prefix=name_prefix,
        )

        components = SimpleNamespace(importer=importer)

        import_stats = await run_import_loop(
            rtype,
            components,  # type: ignore[arg-type]
            resources,
            state,
            dry_run=dry_run,
            on_imported=lambda: None,
            on_skipped=lambda: None,
            on_failed=lambda: None,
        )

        per_type[rtype] = {
            "imported": import_stats.imported,
            "skipped": import_stats.skipped,
            "failed": import_stats.failed,
        }
        totals["imported"] += import_stats.imported
        totals["skipped"] += import_stats.skipped
        totals["failed"] += import_stats.failed

        if log:
            log(
                f"  {rtype}: {import_stats.imported} imported, "
                f"{import_stats.skipped} skipped, {import_stats.failed} failed"
            )

    if log:
        log(
            f"Import complete: {totals['imported']} imported, "
            f"{totals['skipped']} skipped, {totals['failed']} failed"
        )

    return {
        "input_dir": str(in_dir),
        "total_imported": totals["imported"],
        "total_skipped": totals["skipped"],
        "total_failed": totals["failed"],
        "resource_types": per_type,
    }
