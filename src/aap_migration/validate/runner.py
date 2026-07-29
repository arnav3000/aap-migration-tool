"""Post-migration validation engine.

Compares source exports (AAP 2.4) against either:
  - migration database state (default), or
  - the live AAP target API with --live (identity/name match), using the
    migration DB to explain unmatched objects (failed/skipped vs unexplained)

Usage via CLI:
    aap-bridge validate
    aap-bridge validate --live
    aap-bridge validate --live --skip-hosts
    aap-bridge validate --live -r credentials
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aap_migration.migration.database import get_session
from aap_migration.migration.models import IDMapping, MigrationProgress
from aap_migration.utils.logging import get_logger
from aap_migration.validate.models import (
    AuditorCrossCheck,
    AuditorDetail,
    ExclusionSets,
    ExecutiveSummary,
    FieldFinding,
    InventoryCountDetail,
    MissingDetail,
    ObjectEntry,
    OrgTypeRollup,
    OrgValidationSummary,
    PerInventoryCountParity,
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
    "local_path", "client_id",
    "opa_query_path",
    "inventory_sources_with_failures", "total_hosts",
    "capacity", "jobs_total", "policy_instance_percentage",
    "next_run", "last_run",
    "last_login",
}

# Export/API alias → canonical comparison field name
FIELD_ALIASES = {
    "_credentials": "credentials",
}

# FK scalar fields → resource type used for id→name fallback lookup.
# Primary resolution uses summary_fields[field].name on the object itself.
FK_FIELDS: dict[str, str | None] = {
    "organization": "organizations",
    "inventory": "inventories",
    "project": "projects",
    "credential": "credentials",
    "credential_type": "credential_types",
    "source_project": "projects",
    "execution_environment": "execution_environments",
    "instance_group": "instance_groups",
    "inventory_source": "inventory_sources",
    "workflow_job_template": "workflow_job_templates",
    "job_template": "job_templates",
    "team": "teams",
    "user": "users",
    "notification_template": "notification_templates",
    "unified_job_template": None,
}

_ORG_SCOPED_TYPES = {
    "projects", "inventories", "credentials", "job_templates",
    "workflow_job_templates", "teams", "notification_templates",
    "execution_environments", "labels", "applications",
}

# T4 host field sample: Cochran (99% confidence, 4.5% MoE) + fixed seed
HOST_SAMPLE_SEED = 42
HOST_SAMPLE_Z = 2.576
HOST_SAMPLE_MOE = 0.045

_UNSCOPED_TYPES = {
    "users", "organizations", "credential_types", "instance_groups",
    "instances", "settings",
}

# Resource types whose list payloads omit nested schedules / notification
# associations — validate re-fetches related endpoints for live compare.
_RELATED_NESTED_TYPES: dict[str, dict[str, Any]] = {
    "job_templates": {
        "api": "job_templates",
        "schedules": True,
        "notifications": ["started", "success", "error"],
    },
    "workflow_job_templates": {
        "api": "workflow_job_templates",
        "schedules": True,
        "notifications": ["started", "success", "error", "approvals"],
    },
    "projects": {
        "api": "projects",
        "schedules": True,
        "notifications": [],
    },
    "inventory_sources": {
        "api": "inventory_sources",
        "schedules": True,
        "notifications": [],
    },
}

_NOTIF_EVENT_LABELS = {
    "notification_templates_started": "started",
    "notification_templates_success": "success",
    "notification_templates_error": "error",
    "notification_templates_approvals": "approvals",
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


# Child types that inherit organization from an inventory parent
_INVENTORY_CHILD_TYPES = {"hosts", "inventory_groups", "inventory_sources"}


def _inventory_id_from_obj(obj: dict) -> int | None:
    """Best-effort inventory FK id from a child object."""
    sf = (obj.get("summary_fields") or {}).get("inventory")
    if isinstance(sf, dict) and sf.get("id") is not None:
        try:
            return int(sf["id"])
        except (TypeError, ValueError):
            pass
    raw = obj.get("inventory")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _build_inventory_org_maps(
    exports: dict[str, list[dict]],
) -> tuple[dict[str, tuple[str, Optional[int]]], dict[int, tuple[str, Optional[int]]]]:
    """inventory name/id → (org_name, org_id) for parent-org resolution."""
    by_name: dict[str, tuple[str, Optional[int]]] = {}
    by_id: dict[int, tuple[str, Optional[int]]] = {}
    for inv in exports.get("inventories", []):
        org_name, org_id = _get_org_info(inv)
        if not org_name:
            continue
        name = _object_display_name(inv)
        iid = _object_source_id(inv)
        if name:
            by_name[name] = (org_name, org_id)
        if iid is not None:
            by_id[iid] = (org_name, org_id)
    return by_name, by_id


def _org_from_inventory_parent(
    obj: dict,
    inv_org_by_name: dict[str, tuple[str, Optional[int]]],
    inv_org_by_id: dict[int, tuple[str, Optional[int]]],
) -> tuple[str, Optional[int]]:
    inv_name = _parent_ref_name(obj, "inventory")
    if inv_name and inv_name in inv_org_by_name:
        return inv_org_by_name[inv_name]
    iid = _inventory_id_from_obj(obj)
    if iid is not None and iid in inv_org_by_id:
        return inv_org_by_id[iid]
    return "", None


def _build_ujt_org_map(
    exports: dict[str, list[dict]],
    inv_org_by_name: dict[str, tuple[str, Optional[int]]],
    inv_org_by_id: dict[int, tuple[str, Optional[int]]],
) -> dict[str, tuple[str, Optional[int]]]:
    """unified_job_template name → org (JT / WJT / project / inventory source)."""
    ujt: dict[str, tuple[str, Optional[int]]] = {}
    for rtype in ("job_templates", "workflow_job_templates", "projects"):
        for obj in exports.get(rtype, []):
            org_name, org_id = _get_org_info(obj)
            name = _object_display_name(obj)
            if org_name and name:
                ujt[name] = (org_name, org_id)
    for obj in exports.get("inventory_sources", []):
        name = _object_display_name(obj)
        if not name:
            continue
        org_name, org_id = _org_from_inventory_parent(obj, inv_org_by_name, inv_org_by_id)
        if org_name:
            ujt[name] = (org_name, org_id)
    return ujt


def _resolve_object_org(
    rtype: str,
    obj: dict,
    inv_org_by_name: dict[str, tuple[str, Optional[int]]],
    inv_org_by_id: dict[int, tuple[str, Optional[int]]],
    ujt_org_by_name: dict[str, tuple[str, Optional[int]]],
) -> tuple[str, Optional[int]]:
    """Resolve organization, including via inventory / unified_job_template parents."""
    org_name, org_id = _get_org_info(obj)
    if org_name:
        return org_name, org_id

    if rtype in _INVENTORY_CHILD_TYPES:
        return _org_from_inventory_parent(obj, inv_org_by_name, inv_org_by_id)

    if rtype == "schedules":
        parent = _parent_ref_name(obj, "unified_job_template")
        if parent and parent in ujt_org_by_name:
            return ujt_org_by_name[parent]
        return "", None

    if rtype == "workflow_job_template_nodes":
        parent = _parent_ref_name(obj, "workflow_job_template")
        if parent and parent in ujt_org_by_name:
            return ujt_org_by_name[parent]
        return "", None

    return "", None


def _object_display_name(obj: dict) -> str:
    return (
        obj.get("name")
        or obj.get("username")
        or obj.get("hostname")
        or ""
    )


def _parent_ref_name(obj: dict, field: str) -> str:
    """Resolve a parent FK to a display name for identity keys."""
    sf = (obj.get("summary_fields") or {}).get(field)
    if isinstance(sf, dict):
        name = sf.get("name") or sf.get("username") or sf.get("hostname")
        if name:
            return str(name)
    alt = obj.get(f"{field}_name")
    if alt:
        return str(alt)
    return ""


def _object_identity_key(rtype: str, obj: dict) -> tuple:
    """Stable identity for matching export objects to live target objects.

    Uses names (and parent names) — never remapped primary-key IDs.
    """
    name = _object_display_name(obj)
    org_name, _ = _get_org_info(obj)

    if rtype == "users":
        return ("users", obj.get("username") or name)
    if rtype == "organizations":
        return ("organizations", name)
    if rtype in ("credential_types", "instance_groups", "instances"):
        return (rtype, name)
    if rtype == "hosts":
        return ("hosts", _parent_ref_name(obj, "inventory"), name)
    if rtype == "inventory_groups":
        return ("inventory_groups", _parent_ref_name(obj, "inventory"), name)
    if rtype == "inventory_sources":
        return ("inventory_sources", _parent_ref_name(obj, "inventory"), name)
    if rtype == "schedules":
        return (
            "schedules",
            _parent_ref_name(obj, "unified_job_template"),
            name,
        )
    if rtype == "workflow_job_template_nodes":
        return (
            "workflow_job_template_nodes",
            _parent_ref_name(obj, "workflow_job_template"),
            name,
        )
    if rtype in _ORG_SCOPED_TYPES:
        return (rtype, org_name, name)
    if org_name:
        return (rtype, org_name, name)
    return (rtype, name)


def _object_target_id(obj: dict) -> int | None:
    tid = obj.get("id")
    if tid is None:
        return None
    try:
        return int(tid)
    except (TypeError, ValueError):
        return None


def _object_source_id(obj: dict) -> int | None:
    sid = obj.get("_source_id") if obj.get("_source_id") is not None else obj.get("id")
    if sid is None:
        return None
    try:
        return int(sid)
    except (TypeError, ValueError):
        return None


def _classify_unmatched_gap(
    status_by_id: dict[tuple[str, int], tuple[str, str]],
    rtype: str,
    sid: int,
    *,
    live_mode: bool,
) -> tuple[str, str, str]:
    """Classify an unmatched source object for gap accounting and object inventory.

    Returns (bucket, object_status, explanation) where:
      bucket: "failed" | "skipped" | "unexplained"
      object_status: ObjectEntry status (failed/skipped/pending) — never conflates
        unexplained live gaps with import failures
    """
    status_info = status_by_id.get((rtype, sid))
    if status_info:
        status_val, err_msg = status_info
        if status_val == "failed":
            explanation = f"Failed: {err_msg}" if err_msg else "Failed"
            if live_mode:
                explanation = f"Not found on live target ({explanation})"
            return "failed", "failed", explanation
        if status_val == "skipped":
            explanation = f"Skipped: {err_msg}" if err_msg else "Skipped"
            if live_mode:
                explanation = f"Not found on live target ({explanation})"
            return "skipped", "skipped", explanation
        if status_val == "pending":
            explanation = "Pending migration"
            if live_mode:
                explanation = f"Not found on live target ({explanation})"
            return "unexplained", "pending", explanation
        explanation = (
            f"Status: {status_val}" if status_val else "Not tracked in migration DB"
        )
        if live_mode:
            explanation = f"Not found on live target ({explanation})"
        return "unexplained", "pending", explanation

    if live_mode:
        return "unexplained", "pending", "Not found on live target"
    return "unexplained", "pending", "Not tracked in migration DB"


def _build_id_to_name_maps(
    objects_by_type: dict[str, list[dict]],
) -> dict[str, dict[int, str]]:
    """Build resource_type → {id: display_name} lookups."""
    maps: dict[str, dict[int, str]] = {}
    for rtype, objects in objects_by_type.items():
        id_map: dict[int, str] = {}
        for obj in objects:
            oid = _object_source_id(obj)
            if oid is None:
                # Live API objects only have `id` (no _source_id) — already handled
                # by _object_source_id via obj["id"].
                continue
            name = _object_display_name(obj)
            if name:
                id_map[oid] = name
        if id_map:
            maps[rtype] = id_map
    return maps


def _comparable_field_names(obj: dict) -> list[str]:
    """Column names for comparison, with aliases applied (e.g. _credentials → credentials)."""
    cols: set[str] = set()
    for key in obj.keys():
        if key in FIELD_PRUNE:
            continue
        # Private/export-only underscore fields (except aliased ones) stay out
        if key.startswith("_") and key not in FIELD_ALIASES:
            continue
        cols.add(FIELD_ALIASES.get(key, key))
    cols -= set(FIELD_ALIASES.keys())

    # Job templates often only expose associated credentials via summary_fields
    # or the export-only `_credentials` id list — always compare as `credentials`.
    sf = obj.get("summary_fields") or {}
    if (
        "_credentials" in obj
        or "credentials" in obj
        or (isinstance(sf, dict) and "credentials" in sf)
    ):
        cols.add("credentials")

    return sorted(cols)


def _names_from_summary_list(obj: dict, field: str) -> list[str] | None:
    sf = (obj.get("summary_fields") or {}).get(field)
    if not isinstance(sf, list):
        return None
    names: list[str] = []
    for item in sf:
        if isinstance(item, dict):
            name = item.get("name") or item.get("username") or item.get("hostname")
            if name:
                names.append(str(name))
    return sorted(names) if names else []


def _resolve_id_list_to_names(
    raw: Any,
    resource_type: str,
    name_maps: dict[str, dict[int, str]] | None = None,
) -> list[str] | None:
    """Resolve a list of IDs or {id,name} dicts to sorted display names."""
    if not isinstance(raw, list):
        return None
    names: list[str] = []
    id_map = (name_maps or {}).get(resource_type, {})
    for item in raw:
        if isinstance(item, dict):
            name = item.get("name") or item.get("username") or item.get("hostname")
            if name:
                names.append(str(name))
                continue
            rid = item.get("id")
        else:
            rid = item
        try:
            rid_int = int(rid)
        except (TypeError, ValueError):
            names.append(str(rid))
            continue
        looked = id_map.get(rid_int)
        names.append(looked if looked is not None else str(rid_int))
    return sorted(names)


def _credentials_display_value(
    obj: dict,
    name_maps: dict[str, dict[int, str]] | None = None,
) -> list[str]:
    """Job-template credentials as sorted names (never raw ID lists)."""
    from_summary = _names_from_summary_list(obj, "credentials")
    if from_summary is not None and from_summary:
        return from_summary

    for key in ("credentials", "_credentials"):
        raw = obj.get(key)
        resolved = _resolve_id_list_to_names(raw, "credentials", name_maps)
        if resolved is not None:
            return resolved

    if from_summary is not None:
        return from_summary
    return []


def _schedules_display_value(obj: dict) -> list[dict[str, Any]]:
    """Normalize nested schedules to comparable {name, rrule, enabled} rows."""
    raw = obj.get("schedules")
    if not isinstance(raw, list) or not raw:
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rows.append({
            "name": str(item.get("name") or ""),
            "rrule": str(item.get("rrule") or ""),
            "enabled": bool(item.get("enabled", False)),
        })
    return sorted(rows, key=lambda r: (r["name"], r["rrule"]))


def _notifications_display_value(
    obj: dict,
    name_maps: dict[str, dict[int, str]] | None = None,
) -> dict[str, list[str]]:
    """Normalize notification associations to {event: [template names]}."""
    raw = obj.get("notifications")
    if not isinstance(raw, dict) or not raw:
        return {}

    nt_map = (name_maps or {}).get("notification_templates", {})
    out: dict[str, list[str]] = {}
    for key, val in raw.items():
        label = _NOTIF_EVENT_LABELS.get(
            key, key.replace("notification_templates_", "") or key
        )
        names: list[str] = []
        if not isinstance(val, list):
            continue
        for item in val:
            if isinstance(item, dict):
                name = item.get("name")
                if name:
                    names.append(str(name))
                    continue
                rid = item.get("id")
            else:
                rid = item
            try:
                rid_int = int(rid)
            except (TypeError, ValueError):
                names.append(str(rid))
                continue
            names.append(nt_map.get(rid_int, str(rid_int)))
        if names:
            out[label] = sorted(names)
    return dict(sorted(out.items()))


def _format_finding_value(value: Any) -> str:
    """Human-readable finding value (schedules / notifications / lists)."""
    if value is None:
        return ""
    if isinstance(value, list):
        if not value:
            return "(none)"
        if all(isinstance(x, str) for x in value):
            return ", ".join(value)
        if all(isinstance(x, dict) for x in value):
            lines: list[str] = []
            for row in value:
                name = row.get("name") or "?"
                parts = [str(name)]
                rrule = row.get("rrule") or ""
                if rrule:
                    parts.append(str(rrule))
                if "enabled" in row:
                    parts.append(f"enabled={row['enabled']}")
                lines.append(" · ".join(parts))
            return "\n".join(lines)
        return json.dumps(value, indent=2, default=str)
    if isinstance(value, dict):
        if not value:
            return "(none)"
        # notifications: {event: [names]}
        if all(isinstance(v, list) for v in value.values()):
            lines = []
            for event, names in value.items():
                joined = ", ".join(str(n) for n in names) if names else "(none)"
                lines.append(f"{event}: {joined}")
            return "\n".join(lines)
        return json.dumps(value, indent=2, default=str)
    return str(value)


def _fk_display_value(
    obj: dict,
    field: str,
    name_maps: dict[str, dict[int, str]] | None = None,
) -> Any:
    """Resolve an FK field to its referent name for comparison.

    Preference order:
      1. summary_fields[field].name / username / hostname
      2. id→name map for the FK's resource type
      3. raw value (unresolved — still compared as ID)
    """
    if field == "credentials":
        return _credentials_display_value(obj, name_maps)
    if field == "schedules":
        return _schedules_display_value(obj)
    if field == "notifications":
        return _notifications_display_value(obj, name_maps)

    raw = obj.get(field)
    if field not in FK_FIELDS:
        return raw
    if raw is None or raw == "":
        return raw

    sf = (obj.get("summary_fields") or {}).get(field)
    if isinstance(sf, dict):
        name = sf.get("name") or sf.get("username") or sf.get("hostname")
        if name is not None and name != "":
            return name

    rtype = FK_FIELDS.get(field)
    if rtype and name_maps:
        try:
            rid = int(raw)
        except (TypeError, ValueError):
            return raw
        looked = name_maps.get(rtype, {}).get(rid)
        if looked is not None:
            return looked

    return raw


def _extract_field_values(
    obj: dict,
    cols: list[str],
    name_maps: dict[str, dict[int, str]] | None = None,
) -> list[Any]:
    return [_fk_display_value(obj, c, name_maps) for c in cols]


async def _fetch_related_page(client: Any, path: str) -> list[dict]:
    """GET a related list endpoint; return results (empty on failure)."""
    try:
        resp = await client.get(path, params={"page_size": 200})
    except Exception as exc:
        logger.warning("validate_related_fetch_failed", path=path, error=str(exc))
        return []
    if isinstance(resp, dict):
        results = resp.get("results", [])
        return results if isinstance(results, list) else []
    return []


async def _enrich_related_nested(
    client: Any,
    rtype: str,
    objects: list[dict],
    *,
    concurrency: int = 8,
) -> None:
    """Attach schedules / notification associations onto live list objects."""
    cfg = _RELATED_NESTED_TYPES.get(rtype)
    if not cfg or not objects:
        return

    api = cfg["api"]
    want_schedules = bool(cfg.get("schedules"))
    notif_types: list[str] = list(cfg.get("notifications") or [])
    sem = asyncio.Semaphore(concurrency)

    async def _enrich_one(obj: dict) -> None:
        oid = obj.get("id")
        if oid is None:
            return
        async with sem:
            if want_schedules:
                obj["schedules"] = await _fetch_related_page(
                    client, f"{api}/{oid}/schedules/"
                )
            if notif_types:
                notifications: dict[str, list] = {}
                for event in notif_types:
                    results = await _fetch_related_page(
                        client,
                        f"{api}/{oid}/notification_templates_{event}/",
                    )
                    if results:
                        # Keep full objects so names are available without ID maps
                        notifications[f"notification_templates_{event}"] = results
                obj["notifications"] = notifications

    await asyncio.gather(*(_enrich_one(obj) for obj in objects))
    logger.info(
        "validate_related_enrich_done",
        resource_type=rtype,
        objects=len(objects),
        schedules=want_schedules,
        notification_events=notif_types,
    )


# ---------------------------------------------------------------------------
# Export loading
# ---------------------------------------------------------------------------

def load_exports(
    export_dir: Path,
    skip_types: set[str] | None = None,
) -> dict[str, list[dict]]:
    """Load exported objects from disk.

    Supports two layouts:
      1. Directory-based: exports/{type}/{type}_batch001.json
      2. Flat file: exports/{type}.json

    Args:
        export_dir: Path to exports directory
        skip_types: Resource types to ignore (not loaded from disk)
    """
    exports: dict[str, list[dict]] = {}
    if not export_dir.exists():
        return exports

    skip = skip_types or set()

    for child in sorted(export_dir.iterdir()):
        rtype = child.stem if child.is_file() and child.suffix == ".json" else child.name
        if rtype in skip or rtype in _NON_RESOURCE_EXPORT_KEYS:
            continue
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
    """Build source-side field data from exports.

    FK scalar fields are stored as referent names (not raw IDs) so source vs
    target comparison is identity-stable across remapped primary keys.
    Job-template `_credentials` is compared as `credentials` (sorted names).
    """
    name_maps = _build_id_to_name_maps(exports)
    src_field_data: dict[str, dict] = {}
    for rtype in types:
        src_objects = exports.get(rtype, [])
        if not src_objects:
            continue
        cols = _comparable_field_names(src_objects[0])
        # Union columns across objects so aliases/summary-only fields are covered
        for obj in src_objects[1:]:
            cols = sorted(set(cols) | set(_comparable_field_names(obj)))
        src_by_id: dict[int, list] = {}
        for obj in src_objects:
            sid = _object_source_id(obj)
            if sid is not None:
                src_by_id[sid] = _extract_field_values(obj, cols, name_maps)
        src_field_data[rtype] = {"c": cols, "s": src_by_id}
    return src_field_data


async def fetch_live_target(
    target_client: Any,
    types: list[str],
    exports: dict[str, list[dict]],
) -> tuple[
    dict[str, dict],
    dict[str, dict[int, int]],
    dict[str, int],
    dict[str, list[int]],
    dict[str, list[dict]],
]:
    """Fetch live target objects and match them to exports by identity (name).

    Does not use migration DB id_mappings. Returns:
      field_data: {rtype: {c, s, t}} with target rows keyed by source id
      src_to_tgt: identity-matched source_id → target_id
      target_counts: live object count per type
      extra_target_ids: live target ids with no export identity match
      fetched: raw live objects per type (for T4 host sampling, etc.)
    """
    from aap_migration.client.exceptions import (
        APIError,
        AuthenticationError,
        AuthorizationError,
        NotFoundError,
    )

    field_data: dict[str, dict] = {}
    src_to_tgt: dict[str, dict[int, int]] = {}
    target_counts: dict[str, int] = {}
    extra_target_ids: dict[str, list[int]] = {}
    total_fetched = 0
    total_start = time.monotonic()

    # First pass: fetch all types so FK id→name maps cover cross-type refs
    # regardless of alphabetical fetch order.
    fetched: dict[str, list[dict]] = {}
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
            raise
        except NotFoundError:
            elapsed = time.monotonic() - type_start
            logger.warning("validate_type_not_found", resource_type=rtype, elapsed_s=round(elapsed, 1))
            fetched[rtype] = []
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
            fetched[rtype] = []
            continue
        except Exception as exc:
            elapsed = time.monotonic() - type_start
            logger.warning(
                "validate_fetch_error",
                resource_type=rtype,
                error=str(exc),
                elapsed_s=round(elapsed, 1),
            )
            fetched[rtype] = []
            continue

        elapsed = time.monotonic() - type_start
        total_fetched += len(target_objects)
        logger.info(
            "validate_fetch_complete",
            resource_type=rtype,
            count=len(target_objects),
            elapsed_s=round(elapsed, 1),
        )
        fetched[rtype] = target_objects

    tgt_name_maps = _build_id_to_name_maps(fetched)
    src_name_maps = _build_id_to_name_maps(exports)
    # Do NOT merge source/target id→name maps: the same numeric id refers to
    # different objects on each controller (e.g. source NT 58 = alan-slack-alerts,
    # target NT 58 = sam-email-alerts). Resolve each side with its own map.

    for rtype in types:
        if rtype not in fetched:
            continue
        target_objects = fetched[rtype]

        # Re-fetch nested schedules / notifications omitted from list payloads
        if rtype in _RELATED_NESTED_TYPES:
            await _enrich_related_nested(target_client, rtype, target_objects)

        target_counts[rtype] = len(target_objects)
        src_objects = exports.get(rtype, [])

        if not src_objects and not target_objects:
            src_to_tgt[rtype] = {}
            extra_target_ids[rtype] = []
            continue

        cols: set[str] = set()
        for obj in src_objects:
            cols.update(_comparable_field_names(obj))
        for obj in target_objects:
            cols.update(_comparable_field_names(obj))
        nested_cfg = _RELATED_NESTED_TYPES.get(rtype)
        if nested_cfg:
            if nested_cfg.get("schedules"):
                cols.add("schedules")
            if nested_cfg.get("notifications"):
                cols.add("notifications")
        merged_cols = sorted(cols)

        # Source rows from exports (side-specific name maps)
        src_by_id: dict[int, list] = {}
        for obj in src_objects:
            sid = _object_source_id(obj)
            if sid is not None:
                src_by_id[sid] = _extract_field_values(
                    obj, merged_cols, src_name_maps
                )

        # Index live targets by identity key
        tgt_by_identity: dict[tuple, dict] = {}
        for obj in target_objects:
            key = _object_identity_key(rtype, obj)
            if key not in tgt_by_identity:
                tgt_by_identity[key] = obj
            else:
                logger.warning(
                    "validate_duplicate_identity",
                    resource_type=rtype,
                    identity=key,
                    kept_id=tgt_by_identity[key].get("id"),
                    skipped_id=obj.get("id"),
                )

        mapping: dict[int, int] = {}
        matched_tids: set[int] = set()
        tgt_by_sid: dict[int, list] = {}

        for obj in src_objects:
            sid = _object_source_id(obj)
            if sid is None:
                continue
            key = _object_identity_key(rtype, obj)
            tgt_obj = tgt_by_identity.get(key)
            if tgt_obj is None:
                continue
            tid_int = _object_target_id(tgt_obj)
            if tid_int is None:
                continue
            mapping[sid] = tid_int
            matched_tids.add(tid_int)
            tgt_by_sid[sid] = _extract_field_values(
                tgt_obj, merged_cols, tgt_name_maps
            )

        extras: list[int] = []
        for obj in target_objects:
            tid_int = _object_target_id(obj)
            if tid_int is None:
                continue
            if tid_int not in matched_tids:
                extras.append(tid_int)

        src_to_tgt[rtype] = mapping
        extra_target_ids[rtype] = extras
        field_data[rtype] = {"c": merged_cols, "s": src_by_id, "t": tgt_by_sid}

        logger.info(
            "validate_identity_match",
            resource_type=rtype,
            source=len(src_objects),
            target=len(target_objects),
            matched=len(mapping),
            extra=len(extras),
        )

    total_elapsed = time.monotonic() - total_start
    logger.info(
        "validate_live_fetch_done",
        total_objects=total_fetched,
        total_elapsed_s=round(total_elapsed, 1),
    )
    return field_data, src_to_tgt, target_counts, extra_target_ids, fetched


# ---------------------------------------------------------------------------
# T4 host sampling
# ---------------------------------------------------------------------------

def _cochran_sample_size(
    population: int,
    z: float = HOST_SAMPLE_Z,
    moe: float = HOST_SAMPLE_MOE,
    p: float = 0.5,
) -> int:
    """Finite-population Cochran sample size (rounded, clamped to [1, N])."""
    if population <= 0:
        return 0
    n0 = (z * z * p * (1.0 - p)) / (moe * moe)
    n = n0 / (1.0 + (n0 - 1.0) / population)
    return max(1, min(population, int(round(n))))


def _inventory_name_to_id(objects: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for obj in objects:
        name = _object_display_name(obj)
        oid = obj.get("id")
        if not name or oid is None:
            continue
        try:
            out[str(name)] = int(oid)
        except (TypeError, ValueError):
            continue
    return out


def _stratified_sample(
    strata: dict[str, list[Any]],
    n: int,
    rng: random.Random,
) -> list[Any]:
    """Proportional stratified sample; each non-empty stratum gets ≥1 when n allows."""
    items = {k: list(v) for k, v in strata.items() if v}
    population = sum(len(v) for v in items.values())
    if population <= 0 or n <= 0:
        return []
    n = min(n, population)
    if n == population:
        out: list[Any] = []
        for v in items.values():
            out.extend(v)
        return out

    raw = {k: (len(v) / population) * n for k, v in items.items()}
    alloc = {k: int(math.floor(raw[k])) for k in items}
    if n >= len(items):
        for k in items:
            if alloc[k] == 0:
                alloc[k] = 1
    for k in items:
        alloc[k] = min(alloc[k], len(items[k]))

    total = sum(alloc.values())
    by_remainder = sorted(
        items.keys(),
        key=lambda k: (raw[k] - math.floor(raw[k]), len(items[k])),
        reverse=True,
    )
    while total < n:
        progressed = False
        for k in by_remainder:
            if alloc[k] < len(items[k]):
                alloc[k] += 1
                total += 1
                progressed = True
                if total >= n:
                    break
        if not progressed:
            break

    floor = 1 if n >= len(items) else 0
    while total > n:
        progressed = False
        for k in sorted(items.keys(), key=lambda x: alloc[x], reverse=True):
            if alloc[k] > floor:
                alloc[k] -= 1
                total -= 1
                progressed = True
                if total <= n:
                    break
        if not progressed:
            break

    sample: list[Any] = []
    for k, k_n in alloc.items():
        if k_n > 0:
            sample.extend(rng.sample(items[k], k_n))
    return sample


def build_t4_host_sampling(
    exports: dict[str, list[dict]],
    target_hosts: list[dict],
    field_data: dict[str, dict] | None = None,
    target_inventories: list[dict] | None = None,
    seed: int = HOST_SAMPLE_SEED,
) -> T4HostSampling:
    """Build T4: 100% host existence + per-inventory counts + stratified field sample.

    Host field parity is sampled here (not in T3), per validation methodology.
    """
    src_hosts = [h for h in exports.get("hosts", []) if _object_source_id(h) is not None]
    tgt_hosts = list(target_hosts or [])

    src_by_inv: dict[str, list[dict]] = defaultdict(list)
    for h in src_hosts:
        inv = _parent_ref_name(h, "inventory") or "(no inventory)"
        src_by_inv[inv].append(h)

    tgt_by_inv: dict[str, list[dict]] = defaultdict(list)
    for h in tgt_hosts:
        inv = _parent_ref_name(h, "inventory") or "(no inventory)"
        tgt_by_inv[inv].append(h)

    tgt_by_identity = {_object_identity_key("hosts", h): h for h in tgt_hosts}
    matched_pairs: list[tuple[dict, dict]] = []
    for h in src_hosts:
        key = _object_identity_key("hosts", h)
        tgt = tgt_by_identity.get(key)
        if tgt is not None:
            matched_pairs.append((h, tgt))

    matched = len(matched_pairs)
    missing = max(0, len(src_hosts) - matched)

    src_inv_ids = _inventory_name_to_id(exports.get("inventories", []))
    tgt_inv_ids = _inventory_name_to_id(target_inventories or [])

    all_invs = sorted(set(src_by_inv) | set(tgt_by_inv))
    details: list[InventoryCountDetail] = []
    matching_inv = 0
    mismatching_inv = 0
    for inv in all_invs:
        sc = len(src_by_inv.get(inv, []))
        tc = len(tgt_by_inv.get(inv, []))
        details.append(InventoryCountDetail(
            inventory=inv,
            source_id=src_inv_ids.get(inv),
            target_id=tgt_inv_ids.get(inv),
            source_count=sc,
            target_count=tc,
            delta=sc - tc,
        ))
        if sc == tc:
            matching_inv += 1
        else:
            mismatching_inv += 1

    # Stratified field sample among identity-matched hosts
    matched_by_inv: dict[str, list[int]] = defaultdict(list)
    for src_h, _tgt_h in matched_pairs:
        sid = _object_source_id(src_h)
        if sid is None:
            continue
        inv = _parent_ref_name(src_h, "inventory") or "(no inventory)"
        matched_by_inv[inv].append(sid)

    n_sample = _cochran_sample_size(matched)
    rng = random.Random(seed)
    sampled_sids = _stratified_sample(matched_by_inv, n_sample, rng)

    field_mm = 0
    host_fd = (field_data or {}).get("hosts") or {}
    cols = host_fd.get("c") or []
    src_rows = host_fd.get("s") or {}
    tgt_rows = host_fd.get("t") or {}
    if cols and sampled_sids:
        for sid in sampled_sids:
            src_vals = src_rows.get(sid)
            if src_vals is None:
                src_vals = src_rows.get(str(sid))
            tgt_vals = tgt_rows.get(sid)
            if tgt_vals is None:
                tgt_vals = tgt_rows.get(str(sid))
            if src_vals is None or tgt_vals is None:
                continue
            for i, _col in enumerate(cols):
                sv = src_vals[i] if i < len(src_vals) else None
                tv = tgt_vals[i] if i < len(tgt_vals) else None
                if json.dumps(sv, separators=(",", ":"), default=str) != json.dumps(
                    tv, separators=(",", ":"), default=str
                ):
                    field_mm += 1
                    break

    logger.info(
        "validate_t4_host_sampling",
        source=len(src_hosts),
        target=len(tgt_hosts),
        matched=matched,
        missing=missing,
        inventories=len(all_invs),
        sample_size=len(sampled_sids),
        field_mismatches=field_mm,
        seed=seed,
    )

    return T4HostSampling(
        total_hosts_source=len(src_hosts),
        total_hosts_target=len(tgt_hosts),
        matched_hosts=matched,
        missing_hosts=missing,
        inventories_checked=len(all_invs),
        sample_size=len(sampled_sids),
        field_mismatches_in_sample=field_mm,
        per_inventory_count_parity=PerInventoryCountParity(
            matching=matching_inv,
            mismatching=mismatching_inv,
            details=details,
        ),
    )


# ---------------------------------------------------------------------------
# ValidationResult builder
# ---------------------------------------------------------------------------

def _get_obj_name(exports: dict[str, list[dict]], rtype: str, sid: int) -> str:
    for obj in exports.get(rtype, []):
        if _object_source_id(obj) == sid:
            return _object_display_name(obj) or str(sid)
    return str(sid)


def _compute_field_parity(
    field_data: dict[str, dict],
    exports: dict[str, list[dict]],
    src_to_tgt: dict[str, dict[int, int]],
    obj_to_org: dict[tuple[str, int], tuple[str, Optional[int]]],
) -> tuple[dict[str, T3FieldParity], dict[str, list[FieldFinding]]]:
    """Compare source vs target field values from live field_data.

    Hosts are excluded (T4 stratified sample covers host field parity).
    Returns per-type T3FieldParity and per-type list of FieldFinding.
    """
    per_type_t3: dict[str, T3FieldParity] = {}
    per_type_findings: dict[str, list[FieldFinding]] = {}

    for rtype, td in field_data.items():
        if rtype == "hosts":
            # Field parity for hosts is T4 (stratified sample), not full T3
            per_type_t3[rtype] = T3FieldParity()
            per_type_findings[rtype] = []
            continue

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
                        source_value=_format_finding_value(sv),
                        target_value=_format_finding_value(tv),
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


async def build_auditor_cross_check(
    exports: dict[str, list[dict]],
    target_client: Any | None = None,
    target_users: list[dict] | None = None,
) -> AuditorCrossCheck:
    """Compare source is_system_auditor users to Gateway Platform Auditor roles.

    Source side: export users with is_system_auditor is True.
    Target side: Gateway role_user_assignments for "Platform Auditor", with
    Controller is_system_auditor as a fallback when Gateway listing fails.
    """
    from aap_migration.migration.auditor_roles import list_platform_auditor_user_ids

    source_auditors = [
        u for u in exports.get("users", [])
        if u.get("is_system_auditor") is True
    ]

    # Live target users (by username)
    if target_users is None and target_client is not None:
        try:
            target_users = await target_client.list_resources("users", page_size=200)
        except Exception as exc:
            logger.warning("auditor_target_users_fetch_failed", error=str(exc))
            target_users = []
    target_users = target_users or []

    tgt_by_username: dict[str, dict] = {}
    for u in target_users:
        uname = u.get("username")
        if uname:
            tgt_by_username[str(uname)] = u

    gateway_ids: set[int] = set()
    gateway_error: str | None = None
    if target_client is not None:
        _, gateway_ids, gateway_error = await list_platform_auditor_user_ids(
            target_client
        )

    # Controller-flag set used only when Gateway listing fails
    controller_auditor_ids: set[int] = {
        int(u["id"])
        for u in target_users
        if u.get("is_system_auditor") is True and u.get("id") is not None
    }
    if gateway_error is not None:
        logger.warning(
            "auditor_using_controller_fallback",
            error=gateway_error,
        )
        assigned_ids = controller_auditor_ids
        assignment_source = "controller"
    else:
        assigned_ids = gateway_ids
        assignment_source = "gateway"

    details: list[AuditorDetail] = []
    source_usernames: set[str] = set()

    for src in source_auditors:
        username = str(src.get("username") or "")
        if not username:
            continue
        source_usernames.add(username)
        sid = _object_source_id(src)
        tgt = tgt_by_username.get(username)
        tid: int | None = None
        if tgt and tgt.get("id") is not None:
            try:
                tid = int(tgt["id"])
            except (TypeError, ValueError):
                tid = None
        has_assignment = tid is not None and tid in assigned_ids
        details.append(AuditorDetail(
            username=username,
            source_id=sid,
            target_id=tid,
            source_is_system_auditor=True,
            gateway_has_platform_auditor=has_assignment,
            match=has_assignment,
        ))

    # Extra assignees on target not present as source system auditors
    tid_to_username = {
        int(u["id"]): str(u.get("username") or "")
        for u in target_users
        if u.get("id") is not None and u.get("username")
    }
    for tid in sorted(assigned_ids):
        username = tid_to_username.get(tid, "")
        if username and username in source_usernames:
            continue
        details.append(AuditorDetail(
            username=username or f"user-{tid}",
            source_id=None,
            target_id=tid,
            source_is_system_auditor=False,
            gateway_has_platform_auditor=True,
            match=False,
        ))

    mismatches = sum(1 for d in details if not d.match)
    result = AuditorCrossCheck(
        source_system_auditors=len(source_usernames),
        gateway_platform_auditors=len(assigned_ids),
        mismatches=mismatches,
        details=details,
    )
    logger.info(
        "auditor_cross_check_done",
        source=result.source_system_auditors,
        assigned=result.gateway_platform_auditors,
        mismatches=result.mismatches,
        assignment_source=assignment_source,
        gateway_error=gateway_error,
    )
    return result


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
    live_target_counts: dict[str, int] | None = None,
    live_extra_ids: dict[str, list[int]] | None = None,
    auditor_cross_check: AuditorCrossCheck | None = None,
    t4_host_sampling: T4HostSampling | None = None,
) -> ValidationResult:
    """Build ValidationResult from exports + DB state, or exports vs live target."""
    now = datetime.now(timezone.utc)
    live_mode = mode == "validate-live" and live_target_counts is not None
    t4 = t4_host_sampling or T4HostSampling()
    t4_ran = bool(
        t4.total_hosts_source or t4.total_hosts_target or t4.inventories_checked
        or t4.sample_size
    )
    if mode == "validate-live":
        tiers = ["T1", "T2", "T3-live"]
        if t4_ran:
            tiers.append("T4")
    else:
        tiers = ["T1", "T2", "T3-db-status"]

    meta = ValidationMetadata(
        run_id=f"val-{now.strftime('%Y%m%d-%H%M%S')}",
        mode=mode,
        started_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        completed_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        source_url=source_url,
        target_url=target_url,
        tiers_run=tiers,
        read_only=True,
        comparison_rules_version="1.1",
        exclusion_sets=ExclusionSets(
            metadata_fields=len(FIELD_PRUNE),
            fk_fields_by_name=len(FK_FIELDS),
        ),
        host_sample_size=t4.sample_size if t4_ran else 0,
        host_sample_seed=HOST_SAMPLE_SEED if t4_ran else 0,
    )

    obj_to_org: dict[tuple[str, int], tuple[str, Optional[int]]] = {}
    inv_org_by_name, inv_org_by_id = _build_inventory_org_maps(exports)
    ujt_org_by_name = _build_ujt_org_map(exports, inv_org_by_name, inv_org_by_id)
    for rtype, objects in exports.items():
        for obj in objects:
            sid = _object_source_id(obj)
            if sid is None:
                continue
            org_name, org_id = _resolve_object_org(
                rtype, obj, inv_org_by_name, inv_org_by_id, ujt_org_by_name,
            )
            if org_name:
                obj_to_org[(rtype, sid)] = (org_name, org_id)

    # Build status lookup from migration_progress for missing explanations
    # (DB mode and live mode when import inventory is provided)
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
    total_extra = 0
    total_field_mm = 0
    total_explained = 0
    types_with_unexplained = 0

    for rtype in types:
        stats = all_stats.get(rtype, {})
        mapping = src_to_tgt.get(rtype, {})
        src_objects = exports.get(rtype, [])
        src_count = sum(1 for o in src_objects if _object_source_id(o) is not None)

        matched = len(mapping)
        missing = max(0, src_count - matched)
        if live_mode:
            tgt_count = live_target_counts.get(rtype, 0)
            extra_count = len((live_extra_ids or {}).get(rtype, []))
        else:
            # Target = successfully present on AAP (imported or skipped-as-existing)
            tgt_count = stats.get("completed", 0) + stats.get("skipped", 0)
            extra_count = 0

        # Build missing_details for unmatched objects only.
        # Explained gaps = unmatched objects with failed/skipped import status
        # (matched skips like "already exists" are not gaps).
        # Live mode uses the same DB status when available.
        missing_details: list[MissingDetail] = []
        explained_failures = 0
        explained_skips = 0
        unexplained = 0
        for obj in src_objects:
            sid = _object_source_id(obj)
            if sid is None:
                continue
            if sid not in mapping:
                obj_name = _object_display_name(obj) or str(sid)
                org_info = obj_to_org.get((rtype, sid))
                org_name = org_info[0] if org_info else ""
                bucket, _obj_status, explanation = _classify_unmatched_gap(
                    status_by_id, rtype, sid, live_mode=live_mode,
                )
                if bucket == "failed":
                    explained_failures += 1
                elif bucket == "skipped":
                    explained_skips += 1
                else:
                    unexplained += 1
                missing_details.append(MissingDetail(
                    name=obj_name,
                    organization=org_name,
                    parent_type=rtype,
                    source_id=sid,
                    explanation=explanation,
                ))
        explained = explained_failures + explained_skips

        t3 = per_type_t3.get(rtype, T3FieldParity())
        type_field_mm = t3.mismatching

        per_type_results.append(PerTypeResult(
            resource_type=rtype,
            display_name=rtype.replace("_", " ").title(),
            t1_counts=T1Counts(
                source=src_count,
                target=tgt_count,
                delta=src_count - tgt_count,
                explained_failures=explained_failures,
                explained_skips=explained_skips,
                unexplained=unexplained,
            ),
            t2_existence=T2Existence(
                matched=matched,
                missing_on_target=missing,
                extra_on_target=extra_count,
                missing_details=missing_details,
            ),
            t3_field_parity=t3,
        ))

        total_missing += missing
        total_extra += extra_count
        total_field_mm += type_field_mm
        total_explained += explained
        if unexplained > 0:
            types_with_unexplained += 1

        # Per-org tracking
        for obj in src_objects:
            sid = _object_source_id(obj)
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

    # Build OrgTypeRollup and compute unexplained / explained gap counts per org.
    # Explained failures still mark org health red (they are import failures).
    for org_key, org_summary in per_org_data.items():
        explained_failures = 0
        explained_skips = 0
        for md in org_summary.missing_details:
            expl = md.explanation or ""
            if expl.startswith("Failed") or "(Failed" in expl:
                explained_failures += 1
            elif expl.startswith("Skipped") or "(Skipped" in expl:
                explained_skips += 1
        org_summary.explained_failures = explained_failures
        org_summary.explained_skips = explained_skips
        org_summary.unexplained = max(
            0, org_summary.missing - explained_failures - explained_skips
        )
        for rtype, counts in sorted(org_type_counts.get(org_key, {}).items()):
            org_summary.per_type.append(OrgTypeRollup(
                resource_type=rtype,
                source=counts["source"],
                matched=counts["matched"],
                missing=counts["missing"],
                field_mismatches=counts["field_mismatches"],
            ))

    # Build object inventory
    object_inventory: dict[str, list[ObjectEntry]] = {}
    for rtype in types:
        entries: list[ObjectEntry] = []
        mapping = src_to_tgt.get(rtype, {})

        if live_mode:
            for obj in exports.get(rtype, []):
                sid = _object_source_id(obj)
                if sid is None:
                    continue
                obj_name = _object_display_name(obj)
                org_info = obj_to_org.get((rtype, sid))
                org_name = org_info[0] if org_info else (
                    GLOBAL_ORG if rtype in _UNSCOPED_TYPES else ""
                )
                tid = mapping.get(sid)
                if tid is not None:
                    entries.append(ObjectEntry(
                        name=obj_name,
                        organization=org_name,
                        source_id=sid,
                        target_id=tid,
                        status="completed",
                        error="",
                    ))
                else:
                    # Same classification as T1/T2 gap accounting (do not mix
                    # unexplained live gaps into Failed)
                    _bucket, obj_status, explanation = _classify_unmatched_gap(
                        status_by_id, rtype, sid, live_mode=True,
                    )
                    entries.append(ObjectEntry(
                        name=obj_name,
                        organization=org_name,
                        source_id=sid,
                        status=obj_status,
                        error=explanation[:200],
                    ))
        else:
            inv_sids: set[int] = set()
            for sid, sname, status, err, tid in obj_inventory.get(rtype, []):
                inv_sids.add(sid)
                org_info = obj_to_org.get((rtype, sid))
                org_name = org_info[0] if org_info else (
                    GLOBAL_ORG if rtype in _UNSCOPED_TYPES else ""
                )
                entries.append(ObjectEntry(
                    name=sname or "",
                    organization=org_name,
                    source_id=sid,
                    target_id=tid,
                    status=status,
                    error=err[:200] if err else "",
                ))
            for obj in exports.get(rtype, []):
                sid = _object_source_id(obj)
                if sid is None or sid in inv_sids:
                    continue
                obj_name = _object_display_name(obj)
                org_info = obj_to_org.get((rtype, sid))
                org_name = org_info[0] if org_info else (
                    GLOBAL_ORG if rtype in _UNSCOPED_TYPES else ""
                )
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
            total_extra_on_target=total_extra,
            total_field_mismatches=total_field_mm,
            total_explained=total_explained,
            verdict=verdict,
        ),
        per_type=per_type_results,
        per_org=per_org_data,
        object_inventory=object_inventory,
        t4_host_sampling=t4,
        auditor_cross_check=auditor_cross_check or AuditorCrossCheck(),
    )


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

# Export artifacts that are not AAP listable resource collections
_NON_RESOURCE_EXPORT_KEYS = {"metadata", "settings"}


async def run_validation(
    config: Any,
    migration_state: Any | None = None,
    target_client: Any | None = None,
    live: bool = False,
    resource_type: str | None = None,
    skip_hosts: bool = False,
) -> tuple[ValidationResult, dict | None]:
    """Run post-migration validation and return (result, field_data).

    Args:
        config: MigrationConfig instance
        migration_state: MigrationState instance (required for DB mode;
            used with --live to classify unmatched objects via import status)
        target_client: AAPTargetClient (required when live=True)
        live: Fetch live field data from AAP 2.6 API; match by identity (name),
            not import id_mappings
        resource_type: Validate specific type only (default: all)
        skip_hosts: Exclude hosts from validation (no live list, no T4)
    """
    export_dir = Path(config.paths.export_dir)
    logger.info("validate_start", export_dir=str(export_dir), live=live, skip_hosts=skip_hosts)

    skip_types = {"hosts"} if skip_hosts else None
    exports = load_exports(export_dir, skip_types=skip_types)
    # Drop non-resource export files (metadata.json, settings blob, …)
    for key in list(exports.keys()):
        if key in _NON_RESOURCE_EXPORT_KEYS:
            exports.pop(key, None)

    if skip_hosts:
        logger.info("validate_skip_hosts")

    logger.info("validate_exports_loaded", types=len(exports),
                total_objects=sum(len(v) for v in exports.values()))

    if live:
        if target_client is None:
            raise ValueError("--live requires target API access (target_client)")

        types = sorted(exports.keys())
        if resource_type:
            if resource_type not in types:
                raise ValueError(f"Resource type '{resource_type}' not found in exports")
            types = [resource_type]
        elif skip_hosts:
            types = [t for t in types if t != "hosts"]

        field_data, src_to_tgt, target_counts, extra_ids, fetched = await fetch_live_target(
            target_client, types, exports,
        )

        # Import DB explains unmatched objects (failed/skipped vs unexplained)
        all_stats: dict[str, dict] = {}
        obj_inventory: dict[str, list] = {}
        if migration_state is not None:
            for rtype in types:
                all_stats[rtype] = migration_state.get_migration_stats(rtype)
            obj_inventory = _query_object_inventory(migration_state.database_url, types)
            logger.info(
                "validate_live_db_status_loaded",
                types=len(types),
                tracked=sum(len(v) for v in obj_inventory.values()),
            )
        else:
            logger.warning(
                "validate_live_no_migration_db",
                detail="Unmatched objects will all count as unexplained gaps",
            )

        # Always run auditor check when validating live (needs users on both sides)
        auditor_check = await build_auditor_cross_check(
            exports,
            target_client=target_client,
            target_users=None,  # re-lists users; cheap vs full validate
        )

        t4 = T4HostSampling()
        if "hosts" in types:
            t4 = build_t4_host_sampling(
                exports,
                fetched.get("hosts", []),
                field_data=field_data,
                target_inventories=fetched.get("inventories", []),
            )

        result = build_validation_result(
            exports=exports,
            all_stats=all_stats,
            src_to_tgt=src_to_tgt,
            types=types,
            obj_inventory=obj_inventory,
            mode="validate-live",
            source_url=config.source.url,
            target_url=config.target.url,
            field_data=field_data,
            live_target_counts=target_counts,
            live_extra_ids=extra_ids,
            auditor_cross_check=auditor_check,
            t4_host_sampling=t4,
        )
    else:
        if migration_state is None:
            raise ValueError("DB validation mode requires migration_state")

        db_types = _get_db_resource_types(migration_state.database_url)
        types = sorted(set(list(exports.keys()) + db_types))
        if resource_type:
            if resource_type not in types:
                raise ValueError(
                    f"Resource type '{resource_type}' not found in exports or database"
                )
            types = [resource_type]
        elif skip_hosts:
            types = [t for t in types if t != "hosts"]

        src_to_tgt = build_id_maps(migration_state, types)

        all_stats: dict[str, dict] = {}
        for rtype in types:
            all_stats[rtype] = migration_state.get_migration_stats(rtype)

        obj_inventory = _query_object_inventory(migration_state.database_url, types)
        field_data = None

        result = build_validation_result(
            exports=exports,
            all_stats=all_stats,
            src_to_tgt=src_to_tgt,
            types=types,
            obj_inventory=obj_inventory,
            mode="validate-db",
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
