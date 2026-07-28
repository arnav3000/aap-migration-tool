"""Post-migration validation engine.

Compares source exports (AAP 2.4) against migration database state,
optionally fetching live field data from the AAP 2.6 target API.

Usage via CLI:
    aap-bridge validate
    aap-bridge validate --live
    aap-bridge validate --live -r credentials
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aap_migration.migration.database import get_session
from aap_migration.migration.models import IDMapping, MigrationProgress
from aap_migration.utils.logging import get_logger
from aap_migration.validate.models import (
    AuditorCrossCheck,
    ExclusionSets,
    ExecutiveSummary,
    FieldFinding,
    MissingDetail,
    ObjectEntry,
    OrgTypeRollup,
    OrgValidationSummary,
    PerTypeResult,
    T1Counts,
    T2Existence,
    T3FieldParity,
    T4HostSampling,
    ValidationMetadata,
    ValidationResult,
)

logger = get_logger(__name__)

FIELD_PRUNE = {
    "id", "type", "url", "related", "summary_fields", "created", "modified",
    "_source_id", "extra_vars", "survey_spec", "extra_data",
    "notification_configuration", "inputs", "injectors", "variables", "nodes",
    "last_job_run", "next_job_run", "last_job_failed", "status",
    "last_update_failed", "last_updated", "current_job",
}

_ORG_SCOPED_TYPES = {
    "projects", "inventories", "credentials", "job_templates",
    "workflow_job_templates", "teams", "notification_templates",
    "execution_environments", "labels", "applications",
}

_UNSCOPED_TYPES = {
    "users", "organizations", "credential_types", "instance_groups",
    "instances", "settings",
}


def _get_org_info(obj: dict) -> tuple[str, int | None]:
    sf = obj.get("summary_fields") or {}
    org = sf.get("organization") or {}
    org_name = org.get("name", "")
    org_id = org.get("id")
    if not org_name:
        org_name = obj.get("organization_name", "")
    if not org_id:
        org_id = obj.get("organization")
    return org_name, org_id


# ---------------------------------------------------------------------------
# Export loading
# ---------------------------------------------------------------------------

def load_exports(export_dir: Path) -> dict[str, list[dict]]:
    """Load exported objects from disk.

    Supports two layouts:
      1. Directory-based: exports/{type}/{type}_batch001.json
      2. Flat file: exports/{type}.json
    """
    exports: dict[str, list[dict]] = {}
    if not export_dir.exists():
        return exports

    for child in sorted(export_dir.iterdir()):
        rtype = child.stem if child.is_file() and child.suffix == ".json" else child.name
        objects: list[dict] = []

        if child.is_dir():
            for batch_file in sorted(child.glob(f"{rtype}_*.json")):
                try:
                    data = json.loads(batch_file.read_text())
                    if isinstance(data, list):
                        objects.extend(data)
                    else:
                        objects.append(data)
                except Exception as exc:
                    logger.warning("export_batch_read_failed", file=str(batch_file), error=str(exc))
        elif child.is_file() and child.suffix == ".json":
            try:
                data = json.loads(child.read_text())
                if isinstance(data, list):
                    objects = data
                else:
                    objects = [data]
            except Exception as exc:
                logger.warning("export_file_read_failed", file=str(child), error=str(exc))

        if objects:
            exports[rtype] = objects

    return exports


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

def _get_db_resource_types(database_url: str) -> list[str]:
    with get_session(database_url) as session:
        rows = (
            session.query(MigrationProgress.resource_type)
            .distinct()
            .all()
        )
        return sorted(r[0] for r in rows)


def build_id_maps(
    migration_state: Any,
    types: list[str],
) -> dict[str, dict[int, int]]:
    """Build source_id → target_id mappings per resource type."""
    src_to_tgt: dict[str, dict[int, int]] = {}
    for rtype in types:
        src_to_tgt[rtype] = migration_state.get_all_mappings_dict(rtype)
    return src_to_tgt


def _query_object_inventory(
    database_url: str,
    types: list[str],
) -> dict[str, list[tuple]]:
    """Query migration_progress + id_mappings for object inventory."""
    inventory: dict[str, list[tuple]] = {}
    with get_session(database_url) as session:
        for rtype in types:
            rows = (
                session.query(
                    MigrationProgress.source_id,
                    MigrationProgress.source_name,
                    MigrationProgress.status,
                    MigrationProgress.error_message,
                    IDMapping.target_id,
                )
                .outerjoin(
                    IDMapping,
                    (MigrationProgress.resource_type == IDMapping.resource_type)
                    & (MigrationProgress.source_id == IDMapping.source_id),
                )
                .filter(MigrationProgress.resource_type == rtype)
                .order_by(MigrationProgress.source_name)
                .all()
            )
            inventory[rtype] = rows
    return inventory


# ---------------------------------------------------------------------------
# Field data
# ---------------------------------------------------------------------------

def build_field_data(
    exports: dict[str, list[dict]],
    types: list[str],
) -> dict[str, dict]:
    """Build source-side field data from exports."""
    src_field_data: dict[str, dict] = {}
    for rtype in types:
        src_objects = exports.get(rtype, [])
        if not src_objects:
            continue
        sample = src_objects[0]
        cols = sorted(k for k in sample.keys() if k not in FIELD_PRUNE)
        src_by_id: dict[int, list] = {}
        for obj in src_objects:
            sid = obj.get("_source_id") or obj.get("id")
            if sid is not None:
                src_by_id[sid] = [obj.get(c) for c in cols]
        src_field_data[rtype] = {"c": cols, "s": src_by_id}
    return src_field_data


async def fetch_live_target(
    target_client: Any,
    types: list[str],
    src_to_tgt: dict[str, dict[int, int]],
    src_field_data: dict[str, dict],
) -> dict[str, dict]:
    """Fetch actual stored objects from AAP 2.6 API for field comparison."""
    from aap_migration.client.exceptions import (
        APIError,
        AuthenticationError,
        AuthorizationError,
        NotFoundError,
    )

    field_data: dict[str, dict] = {}
    total_fetched = 0
    total_start = time.monotonic()

    for rtype in types:
        type_start = time.monotonic()
        logger.info("validate_fetch_type", resource_type=rtype)
        try:
            target_objects = await target_client.list_resources(rtype, page_size=200)
        except (AuthenticationError, AuthorizationError) as exc:
            elapsed = time.monotonic() - type_start
            logger.error(
                "validate_auth_fatal",
                resource_type=rtype,
                status_code=exc.status_code,
                elapsed_s=round(elapsed, 1),
            )
            for rt in types:
                if rt not in field_data and rt in src_field_data:
                    entry = src_field_data[rt]
                    field_data[rt] = {"c": entry["c"], "s": entry["s"], "t": {}}
            raise
        except NotFoundError:
            elapsed = time.monotonic() - type_start
            logger.warning("validate_type_not_found", resource_type=rtype, elapsed_s=round(elapsed, 1))
            if rtype in src_field_data:
                entry = src_field_data[rtype]
                field_data[rtype] = {"c": entry["c"], "s": entry["s"], "t": {}}
            continue
        except APIError as exc:
            elapsed = time.monotonic() - type_start
            logger.warning(
                "validate_api_error",
                resource_type=rtype,
                status_code=exc.status_code,
                message=exc.message,
                elapsed_s=round(elapsed, 1),
            )
            if rtype in src_field_data:
                entry = src_field_data[rtype]
                field_data[rtype] = {"c": entry["c"], "s": entry["s"], "t": {}}
            continue
        except Exception as exc:
            elapsed = time.monotonic() - type_start
            logger.warning(
                "validate_fetch_error",
                resource_type=rtype,
                error=str(exc),
                elapsed_s=round(elapsed, 1),
            )
            if rtype in src_field_data:
                entry = src_field_data[rtype]
                field_data[rtype] = {"c": entry["c"], "s": entry["s"], "t": {}}
            continue

        elapsed = time.monotonic() - type_start
        total_fetched += len(target_objects)
        logger.info(
            "validate_fetch_complete",
            resource_type=rtype,
            count=len(target_objects),
            elapsed_s=round(elapsed, 1),
        )

        src_entry = src_field_data.get(rtype)
        src_cols = set(src_entry["c"]) if src_entry else set()

        tgt_cols = set()
        if target_objects:
            tgt_cols = {k for k in target_objects[0].keys() if k not in FIELD_PRUNE}

        merged_cols = sorted(src_cols | tgt_cols)

        src_by_id: dict[int, list] = {}
        if src_entry:
            old_cols = src_entry["c"]
            for sid, old_vals in src_entry["s"].items():
                old_map = dict(zip(old_cols, old_vals))
                src_by_id[int(sid) if isinstance(sid, str) else sid] = [
                    old_map.get(c) for c in merged_cols
                ]

        mapping = src_to_tgt.get(rtype, {})
        tgt_by_tid: dict[int, list] = {}
        for obj in target_objects:
            tid = obj.get("id")
            if tid is not None:
                tgt_by_tid[tid] = [obj.get(c) for c in merged_cols]

        tgt_by_sid: dict[int, list] = {}
        for sid, tid in mapping.items():
            if tid in tgt_by_tid:
                tgt_by_sid[sid] = tgt_by_tid[tid]

        field_data[rtype] = {"c": merged_cols, "s": src_by_id, "t": tgt_by_sid}

    total_elapsed = time.monotonic() - total_start
    logger.info(
        "validate_live_fetch_done",
        total_objects=total_fetched,
        total_elapsed_s=round(total_elapsed, 1),
    )
    return field_data


# ---------------------------------------------------------------------------
# ValidationResult builder
# ---------------------------------------------------------------------------

def _get_obj_name(exports: dict[str, list[dict]], rtype: str, sid: int) -> str:
    for obj in exports.get(rtype, []):
        if obj.get("id") == sid:
            return obj.get("name", "") or obj.get("username", "") or str(sid)
    return str(sid)


def _compute_field_parity(
    field_data: dict[str, dict],
    exports: dict[str, list[dict]],
    src_to_tgt: dict[str, dict[int, int]],
    obj_to_org: dict[tuple[str, int], tuple[str, Optional[int]]],
) -> tuple[dict[str, T3FieldParity], dict[str, list[FieldFinding]]]:
    """Compare source vs target field values from live field_data.

    Returns per-type T3FieldParity and per-type list of FieldFinding.
    """
    per_type_t3: dict[str, T3FieldParity] = {}
    per_type_findings: dict[str, list[FieldFinding]] = {}

    for rtype, td in field_data.items():
        cols = td.get("c", [])
        src_rows = td.get("s", {})
        tgt_rows = td.get("t", {})
        if not cols or not tgt_rows:
            per_type_t3[rtype] = T3FieldParity()
            per_type_findings[rtype] = []
            continue

        compared = 0
        matching = 0
        mismatching = 0
        findings: list[FieldFinding] = []

        for sid_key, src_vals in src_rows.items():
            sid = int(sid_key) if isinstance(sid_key, str) else sid_key
            tgt_vals = tgt_rows.get(sid) or tgt_rows.get(sid_key)
            if tgt_vals is None:
                continue

            compared += 1
            obj_name = _get_obj_name(exports, rtype, sid)
            org_info = obj_to_org.get((rtype, sid))
            org_name = org_info[0] if org_info else ""
            tid = src_to_tgt.get(rtype, {}).get(sid)

            has_mismatch = False
            for i, col in enumerate(cols):
                sv = src_vals[i] if i < len(src_vals) else None
                tv = tgt_vals[i] if i < len(tgt_vals) else None
                if json.dumps(sv, separators=(",", ":"), default=str) != json.dumps(tv, separators=(",", ":"), default=str):
                    has_mismatch = True
                    findings.append(FieldFinding(
                        name=obj_name,
                        organization=org_name,
                        source_id=sid,
                        target_id=tid,
                        field=col,
                        source_value=str(sv) if sv is not None else "",
                        target_value=str(tv) if tv is not None else "",
                        tier="T3",
                    ))

            if has_mismatch:
                mismatching += 1
            else:
                matching += 1

        per_type_t3[rtype] = T3FieldParity(
            compared=compared,
            matching=matching,
            mismatching=mismatching,
            findings=findings,
        )
        per_type_findings[rtype] = findings

    return per_type_t3, per_type_findings


def build_validation_result(
    exports: dict[str, list[dict]],
    all_stats: dict[str, dict],
    src_to_tgt: dict[str, dict[int, int]],
    types: list[str],
    obj_inventory: dict[str, list[tuple]],
    mode: str,
    source_url: str = "",
    target_url: str = "",
    field_data: dict[str, dict] | None = None,
) -> ValidationResult:
    """Build ValidationResult from exports + DB state."""
    now = datetime.now(timezone.utc)
    tiers = ["T1", "T2", "T3-live"] if mode == "validate-live" else ["T1", "T2", "T3-db-status"]

    meta = ValidationMetadata(
        run_id=f"val-{now.strftime('%Y%m%d-%H%M%S')}",
        mode=mode,
        started_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        completed_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        source_url=source_url,
        target_url=target_url,
        tiers_run=tiers,
        read_only=True,
        comparison_rules_version="1.0",
        exclusion_sets=ExclusionSets(
            metadata_fields=len(FIELD_PRUNE),
        ),
    )

    obj_to_org: dict[tuple[str, int], tuple[str, Optional[int]]] = {}
    for rtype, objects in exports.items():
        for obj in objects:
            sid = obj.get("id")
            if sid is None:
                continue
            org_name, org_id = _get_org_info(obj)
            if org_name:
                obj_to_org[(rtype, sid)] = (org_name, org_id)

    # Build status lookup from migration_progress for missing explanations
    status_by_id: dict[tuple[str, int], tuple[str, str]] = {}
    for rtype, rows in obj_inventory.items():
        for sid, sname, status, err, tid in rows:
            status_by_id[(rtype, sid)] = (status or "", err or "")

    # Compute T3 field parity if field_data available
    per_type_t3: dict[str, T3FieldParity] = {}
    per_type_findings: dict[str, list[FieldFinding]] = {}
    if field_data:
        per_type_t3, per_type_findings = _compute_field_parity(
            field_data, exports, src_to_tgt, obj_to_org,
        )

    GLOBAL_ORG = "Global / Unscoped"
    per_org_data: dict[str, OrgValidationSummary] = {}
    # Track per-org per-type counts for OrgTypeRollup
    org_type_counts: dict[str, dict[str, dict[str, int]]] = {}
    per_type_results: list[PerTypeResult] = []
    total_missing = 0
    total_field_mm = 0
    total_explained = 0
    types_with_unexplained = 0

    for rtype in types:
        stats = all_stats.get(rtype, {})
        mapping = src_to_tgt.get(rtype, {})
        src_objects = exports.get(rtype, [])
        src_count = sum(1 for o in src_objects if o.get("id") is not None)
        tgt_count = stats.get("completed", 0)
        matched = len(mapping)
        missing = max(0, src_count - matched)
        explained = stats.get("failed", 0) + stats.get("skipped", 0)
        unexplained = max(0, missing - explained)

        # Build missing_details for objects not in id_mappings
        missing_details: list[MissingDetail] = []
        for obj in src_objects:
            sid = obj.get("id")
            if sid is None:
                continue
            if sid not in mapping:
                obj_name = obj.get("name", "") or obj.get("username", "") or str(sid)
                org_info = obj_to_org.get((rtype, sid))
                org_name = org_info[0] if org_info else ""
                status_info = status_by_id.get((rtype, sid))
                if status_info:
                    status_val, err_msg = status_info
                    if status_val == "failed":
                        explanation = f"Failed: {err_msg}" if err_msg else "Failed"
                    elif status_val == "skipped":
                        explanation = f"Skipped: {err_msg}" if err_msg else "Skipped"
                    elif status_val == "pending":
                        explanation = "Pending migration"
                    else:
                        explanation = f"Status: {status_val}" if status_val else "Not tracked in migration DB"
                else:
                    explanation = "Not tracked in migration DB"
                missing_details.append(MissingDetail(
                    name=obj_name,
                    organization=org_name,
                    parent_type=rtype,
                    source_id=sid,
                    explanation=explanation,
                ))

        t3 = per_type_t3.get(rtype, T3FieldParity())
        type_field_mm = t3.mismatching

        per_type_results.append(PerTypeResult(
            resource_type=rtype,
            display_name=rtype.replace("_", " ").title(),
            t1_counts=T1Counts(
                source=src_count,
                target=tgt_count,
                delta=src_count - tgt_count,
                explained_failures=stats.get("failed", 0),
                explained_skips=stats.get("skipped", 0),
                unexplained=unexplained,
            ),
            t2_existence=T2Existence(
                matched=matched,
                missing_on_target=missing,
                missing_details=missing_details,
            ),
            t3_field_parity=t3,
        ))

        total_missing += missing
        total_field_mm += type_field_mm
        total_explained += explained
        if unexplained > 0:
            types_with_unexplained += 1

        # Per-org tracking
        for obj in src_objects:
            sid = obj.get("id")
            if sid is None:
                continue
            org_info = obj_to_org.get((rtype, sid))
            org_key = org_info[0] if org_info else (GLOBAL_ORG if rtype in _UNSCOPED_TYPES else "")
            if not org_key:
                continue

            if org_key not in per_org_data:
                org_id = org_info[1] if org_info else None
                per_org_data[org_key] = OrgValidationSummary(
                    org_name=org_key,
                    source_id=org_id,
                )
                org_type_counts[org_key] = {}

            org_summary = per_org_data[org_key]
            org_summary.total_objects += 1

            is_matched = sid in mapping
            if is_matched:
                org_summary.matched += 1
            else:
                org_summary.missing += 1

            # Track per-org per-type
            if rtype not in org_type_counts[org_key]:
                org_type_counts[org_key][rtype] = {
                    "source": 0, "matched": 0, "missing": 0, "field_mismatches": 0,
                }
            otc = org_type_counts[org_key][rtype]
            otc["source"] += 1
            if is_matched:
                otc["matched"] += 1
            else:
                otc["missing"] += 1

    # Distribute missing_details to per-org
    for ptr in per_type_results:
        for md in ptr.t2_existence.missing_details:
            org_key = md.organization or GLOBAL_ORG
            if org_key in per_org_data:
                per_org_data[org_key].missing_details.append(md)

    # Distribute field_findings to per-org and count per-org per-type field mismatches
    for rtype, findings in per_type_findings.items():
        seen_sids: set[int] = set()
        for ff in findings:
            org_key = ff.organization or GLOBAL_ORG
            if org_key in per_org_data:
                per_org_data[org_key].field_findings.append(ff)
                if ff.source_id not in seen_sids:
                    seen_sids.add(ff.source_id)
                    per_org_data[org_key].field_mismatches += 1
                    if org_key in org_type_counts and rtype in org_type_counts[org_key]:
                        org_type_counts[org_key][rtype]["field_mismatches"] += 1

    # Build OrgTypeRollup and compute unexplained per org
    for org_key, org_summary in per_org_data.items():
        org_summary.unexplained = max(0, org_summary.missing)
        for rtype, counts in sorted(org_type_counts.get(org_key, {}).items()):
            org_summary.per_type.append(OrgTypeRollup(
                resource_type=rtype,
                source=counts["source"],
                matched=counts["matched"],
                missing=counts["missing"],
                field_mismatches=counts["field_mismatches"],
            ))

    # Build object inventory — include exports objects not in migration_progress
    object_inventory: dict[str, list[ObjectEntry]] = {}
    for rtype in types:
        entries: list[ObjectEntry] = []
        inv_sids: set[int] = set()
        for sid, sname, status, err, tid in obj_inventory.get(rtype, []):
            inv_sids.add(sid)
            org_info = obj_to_org.get((rtype, sid))
            org_name = org_info[0] if org_info else ""
            entries.append(ObjectEntry(
                name=sname or "",
                organization=org_name,
                source_id=sid,
                target_id=tid,
                status=status,
                error=err[:200] if err else "",
            ))
        for obj in exports.get(rtype, []):
            sid = obj.get("id")
            if sid is None or sid in inv_sids:
                continue
            obj_name = obj.get("name", "") or obj.get("username", "") or ""
            org_info = obj_to_org.get((rtype, sid))
            org_name = org_info[0] if org_info else ""
            entries.append(ObjectEntry(
                name=obj_name,
                organization=org_name,
                source_id=sid,
                status="pending",
                error="Not tracked in migration DB",
            ))
        object_inventory[rtype] = entries

    total_unexplained = sum(t.t1_counts.unexplained for t in per_type_results)
    verdict = "PASS" if total_unexplained == 0 and total_field_mm == 0 else "REVIEW REQUIRED"

    return ValidationResult(
        metadata=meta,
        executive_summary=ExecutiveSummary(
            total_resource_types=len(per_type_results),
            types_with_unexplained_delta=types_with_unexplained,
            total_missing_on_target=total_missing,
            total_field_mismatches=total_field_mm,
            total_explained=total_explained,
            verdict=verdict,
        ),
        per_type=per_type_results,
        per_org=per_org_data,
        object_inventory=object_inventory,
        t4_host_sampling=T4HostSampling(),
        auditor_cross_check=AuditorCrossCheck(),
    )


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

async def run_validation(
    config: Any,
    migration_state: Any,
    target_client: Any | None = None,
    live: bool = False,
    resource_type: str | None = None,
) -> tuple[ValidationResult, dict | None]:
    """Run post-migration validation and return (result, field_data).

    Args:
        config: MigrationConfig instance
        migration_state: MigrationState instance
        target_client: AAPTargetClient (required when live=True)
        live: Fetch live field data from AAP 2.6 API
        resource_type: Validate specific type only (default: all)
    """
    export_dir = Path(config.paths.export_dir)
    logger.info("validate_start", export_dir=str(export_dir), live=live)

    exports = load_exports(export_dir)
    logger.info("validate_exports_loaded", types=len(exports),
                total_objects=sum(len(v) for v in exports.values()))

    db_types = _get_db_resource_types(migration_state.database_url)
    types = sorted(set(list(exports.keys()) + db_types))
    if resource_type:
        if resource_type not in types:
            raise ValueError(f"Resource type '{resource_type}' not found in exports or database")
        types = [resource_type]

    src_to_tgt = build_id_maps(migration_state, types)

    all_stats: dict[str, dict] = {}
    for rtype in types:
        all_stats[rtype] = migration_state.get_migration_stats(rtype)

    mode = "validate-live" if live else "validate-db"
    field_data: dict | None = None

    if live:
        if target_client is None:
            raise ValueError("--live requires target API access (target_client)")
        src_field_data = build_field_data(exports, types)
        field_data = await fetch_live_target(
            target_client, types, src_to_tgt, src_field_data,
        )

    obj_inventory = _query_object_inventory(migration_state.database_url, types)

    result = build_validation_result(
        exports=exports,
        all_stats=all_stats,
        src_to_tgt=src_to_tgt,
        types=types,
        obj_inventory=obj_inventory,
        mode=mode,
        source_url=config.source.url,
        target_url=config.target.url,
        field_data=field_data,
    )

    if field_data:
        fd_src = sum(
            sum(len(json.dumps(v, separators=(",", ":"))) for v in td["s"].values())
            for td in field_data.values()
        )
        fd_tgt = sum(
            sum(len(json.dumps(v, separators=(",", ":"))) for v in td["t"].values())
            for td in field_data.values()
        )
        logger.info("validate_field_data",
                     src_mb=round(fd_src / 1024 / 1024, 1),
                     tgt_mb=round(fd_tgt / 1024 / 1024, 1))

    return result, field_data
