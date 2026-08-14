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
    aap-bridge validate --orgs Team-alan
    aap-bridge validate --live --orgs Team-alan
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

from aap_migration.client.exceptions import NetworkError, ServerError
from aap_migration.migration.database import get_session
from aap_migration.migration.models import IDMapping, MigrationProgress
from aap_migration.utils.logging import get_logger
from aap_migration.validate.common import (
    FK_REFERENCE_TYPES,
    INVENTORY_CHILD_TYPES,
    ORG_SCOPE_SKIP_TYPES,
    SCHEDULE_PARENT_TYPES,
    UNSCOPED_TYPES,
    WORKFLOW_NODE_TYPES,
    apply_migration_buckets,
    build_executive_summary,
    classify_sync_from_api,
    count_explained_gaps,
)
from aap_migration.validate.models import (
    AuditorCrossCheck,
    AuditorDetail,
    ExclusionSets,
    ExtraDetail,
    FieldFinding,
    InventoryCountDetail,
    MissingDetail,
    ObjectEntry,
    OrgTypeRollup,
    OrgValidationSummary,
    PerInventoryCountParity,
    PerTypeResult,
    SyncEntry,
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
    "last_login", "dtstart", "dtend", "instances",
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

# Extra-on-target tab: list details for unmatched target objects (live only).
# Hosts are counted in T1/T2 extras but omitted from the Extra tab list by default
# (host parity is covered under Hosts / T4).
EXTRA_TAB_EXCLUDE_TYPES = frozenset({"hosts"})
EXTRA_DETAILS_MAX_PER_TYPE = 5000

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

    if rtype in INVENTORY_CHILD_TYPES:
        return _org_from_inventory_parent(obj, inv_org_by_name, inv_org_by_id)

    if rtype == "schedules":
        parent = _parent_ref_name(obj, "unified_job_template")
        if parent and parent in ujt_org_by_name:
            return ujt_org_by_name[parent]
        return "", None

    if rtype in WORKFLOW_NODE_TYPES:
        parent = _parent_ref_name(obj, "workflow_job_template")
        if parent and parent in ujt_org_by_name:
            return ujt_org_by_name[parent]
        return "", None

    return "", None


def parse_orgs_arg(orgs_arg: str | None) -> list[str] | None:
    """Parse comma-separated --orgs value into a list of names."""
    if not orgs_arg or not str(orgs_arg).strip():
        return None
    orgs = [part.strip() for part in str(orgs_arg).split(",") if part.strip()]
    return orgs or None


def _export_organization_names(exports: dict[str, list[dict]]) -> set[str]:
    names: set[str] = set()
    for obj in exports.get("organizations", []):
        name = _object_display_name(obj)
        if name:
            names.add(name)
    return names


def _object_in_org_scope(
    rtype: str,
    obj: dict,
    selected: set[str],
    inv_org_by_name: dict[str, tuple[str, Optional[int]]],
    inv_org_by_id: dict[int, tuple[str, Optional[int]]],
    ujt_org_by_name: dict[str, tuple[str, Optional[int]]],
) -> bool:
    """Whether an export object belongs in an --orgs scoped run.

    - organizations: keep only selected org records
    - pure globals (users, credential types, …): dropped
    - org-owned / parent-scoped: keep when resolved org is selected
    """
    if rtype in ORG_SCOPE_SKIP_TYPES:
        return False
    if rtype == "organizations":
        return _object_display_name(obj) in selected
    org_name, _ = _resolve_object_org(
        rtype, obj, inv_org_by_name, inv_org_by_id, ujt_org_by_name,
    )
    return bool(org_name) and org_name in selected


def filter_exports_by_orgs(
    exports: dict[str, list[dict]],
    organizations: list[str],
    *,
    require_known: bool = True,
) -> dict[str, list[dict]]:
    """Return export objects limited to selected orgs.

    Drops pure global types. Used for DB-mode --orgs and as the source-side
    filter before Plan B live fetch.

    Raises ValueError if require_known and a requested org name is absent.
    """
    selected = set(organizations)
    if require_known:
        known = _export_organization_names(exports)
        missing = sorted(selected - known)
        if missing:
            available = ", ".join(sorted(known)) or "(none)"
            raise ValueError(
                f"Unknown organization(s): {', '.join(missing)}. "
                f"Organizations in exports: {available}"
            )

    inv_org_by_name, inv_org_by_id = _build_inventory_org_maps(exports)
    ujt_org_by_name = _build_ujt_org_map(exports, inv_org_by_name, inv_org_by_id)

    filtered: dict[str, list[dict]] = {}
    for rtype, objects in exports.items():
        if rtype in ORG_SCOPE_SKIP_TYPES:
            continue
        kept = [
            obj
            for obj in objects
            if _object_in_org_scope(
                rtype,
                obj,
                selected,
                inv_org_by_name,
                inv_org_by_id,
                ujt_org_by_name,
            )
        ]
        if kept:
            filtered[rtype] = kept
    return filtered


def extract_fk_reference_exports(
    exports: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """Pull pure-global types used only for FK id→name resolution under --orgs."""
    refs: dict[str, list[dict]] = {}
    for rtype in FK_REFERENCE_TYPES:
        objects = exports.get(rtype) or []
        if objects:
            refs[rtype] = list(objects)
    return refs


def _merge_id_to_name_maps(
    base: dict[str, dict[int, str]],
    extra: dict[str, dict[int, str]],
) -> dict[str, dict[int, str]]:
    """Merge id→name maps; existing keys in base win on id collision."""
    out: dict[str, dict[int, str]] = {k: dict(v) for k, v in base.items()}
    for rtype, id_map in extra.items():
        slot = out.setdefault(rtype, {})
        for oid, name in id_map.items():
            slot.setdefault(oid, name)
    return out


def _filter_obj_inventory_to_exports(
    obj_inventory: dict[str, list[tuple]],
    exports: dict[str, list[dict]],
) -> dict[str, list[tuple]]:
    """Keep DB inventory rows whose source_id remains in filtered exports."""
    allowed: dict[str, set[int]] = {}
    for rtype, objects in exports.items():
        sids = {
            sid
            for sid in (_object_source_id(o) for o in objects)
            if sid is not None
        }
        allowed[rtype] = sids

    out: dict[str, list[tuple]] = {}
    for rtype, rows in obj_inventory.items():
        keep = allowed.get(rtype, set())
        kept = [row for row in rows if row[0] in keep]
        if kept:
            out[rtype] = kept
    return out


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
    details = obj.get(f"{field}_details")
    if isinstance(details, dict) and details.get("name"):
        return str(details["name"])
    return ""


def _object_identity_key(rtype: str, obj: dict) -> tuple:
    """Stable identity for matching export objects to live target objects.

    Uses names (and parent names) — never remapped primary-key IDs.
    Credentials use (org, credential_type, name) to match AAP uniqueness.
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
    if rtype in WORKFLOW_NODE_TYPES:
        return (
            rtype,
            _parent_ref_name(obj, "workflow_job_template"),
            name,
        )
    if rtype == "credentials":
        # AAP uniqueness: (name, organization, credential_type)
        return (
            "credentials",
            org_name,
            _parent_ref_name(obj, "credential_type"),
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


def _prefixed_import_reason(prefix: str, err_msg: str | None) -> str:
    """Format an import gap explanation without duplicating Failed:/Skipped:."""
    text = (err_msg or "").strip()
    if not text:
        return prefix
    marker = f"{prefix.lower()}:"
    if text.lower().startswith(marker):
        return text
    return f"{prefix}: {text}"


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

    In live mode, when migration DB status is available the explanation is the
    import reason only (Failed/Skipped/Pending/Status) — the Missing tab already
    implies the object is absent on the live target. Without DB status, live
    gaps use ``Not found on live target``.
    """
    status_info = status_by_id.get((rtype, sid))
    if status_info:
        status_val, err_msg = status_info
        if status_val == "failed":
            explanation = _prefixed_import_reason("Failed", err_msg)
            return "failed", "failed", explanation
        if status_val == "skipped":
            explanation = _prefixed_import_reason("Skipped", err_msg)
            return "skipped", "skipped", explanation
        if status_val == "pending":
            return "unexplained", "pending", "Pending migration"
        explanation = (
            f"Status: {status_val}" if status_val else "Not tracked in migration DB"
        )
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


async def _list_resources_safe(
    target_client: Any,
    rtype: str,
    *,
    filters: dict[str, Any] | None = None,
    page_size: int = 200,
) -> list[dict]:
    """list_resources with validate's existing soft-fail behaviour."""
    from aap_migration.client.exceptions import (
        APIError,
        AuthenticationError,
        AuthorizationError,
        NotFoundError,
    )

    type_start = time.monotonic()
    logger.info(
        "validate_fetch_type",
        resource_type=rtype,
        filters=filters or {},
    )
    try:
        target_objects = await target_client.list_resources(
            rtype, filters=filters, page_size=page_size,
        )
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
        logger.warning(
            "validate_type_not_found",
            resource_type=rtype,
            elapsed_s=round(elapsed, 1),
        )
        return []
    except APIError as exc:
        elapsed = time.monotonic() - type_start
        logger.warning(
            "validate_api_error",
            resource_type=rtype,
            status_code=exc.status_code,
            message=exc.message,
            elapsed_s=round(elapsed, 1),
        )
        return []
    except Exception as exc:
        elapsed = time.monotonic() - type_start
        logger.warning(
            "validate_fetch_error",
            resource_type=rtype,
            error=str(exc),
            elapsed_s=round(elapsed, 1),
        )
        return []

    elapsed = time.monotonic() - type_start
    logger.info(
        "validate_fetch_complete",
        resource_type=rtype,
        count=len(target_objects),
        elapsed_s=round(elapsed, 1),
        filters=filters or {},
    )
    return target_objects


def _dedupe_by_id(objects: list[dict]) -> list[dict]:
    seen: set[int] = set()
    out: list[dict] = []
    for obj in objects:
        tid = _object_target_id(obj)
        if tid is not None:
            if tid in seen:
                continue
            seen.add(tid)
        out.append(obj)
    return out


async def _resolve_target_org_ids(
    target_client: Any,
    organizations: list[str],
) -> dict[str, int]:
    """Map selected org names → target org IDs (exact name match)."""
    name_to_id: dict[str, int] = {}
    for name in organizations:
        found = await _list_resources_safe(
            target_client, "organizations", filters={"name": name},
        )
        match = next((o for o in found if _object_display_name(o) == name), None)
        if match is None:
            raise ValueError(
                f"Organization '{name}' not found on live target. "
                "Check --orgs spelling against the AAP 2.6 organization name."
            )
        tid = _object_target_id(match)
        if tid is None:
            raise ValueError(f"Organization '{name}' on target has no id")
        name_to_id[name] = tid
    return name_to_id


async def _fetch_by_organization_ids(
    target_client: Any,
    rtype: str,
    org_ids: list[int],
) -> list[dict]:
    """Fetch an org-owned type with organization=<id> per selected org."""
    objects: list[dict] = []
    for oid in org_ids:
        objects.extend(
            await _list_resources_safe(
                target_client, rtype, filters={"organization": oid},
            )
        )
    return _dedupe_by_id(objects)


async def _fetch_children_by_parent_ids(
    target_client: Any,
    rtype: str,
    parent_field: str,
    parent_ids: list[int],
) -> list[dict]:
    """Fetch child resources filtered by parent FK (inventory=, etc.)."""
    objects: list[dict] = []
    for pid in parent_ids:
        objects.extend(
            await _list_resources_safe(
                target_client, rtype, filters={parent_field: pid},
            )
        )
    return _dedupe_by_id(objects)


async def _fetch_live_org_scoped(
    target_client: Any,
    types: list[str],
    organizations: list[str],
) -> dict[str, list[dict]]:
    """Plan B: fetch only selected orgs' objects (and their children).

    - Skip pure globals
    - Org-owned types: ?organization=<id>
    - Inventory children: ?inventory=<id> for in-scope inventories
    - Schedules: ?unified_job_template=<id> for in-scope parents
    - Workflow nodes: nested under in-scope WJTs
    """
    org_name_to_id = await _resolve_target_org_ids(target_client, organizations)
    org_ids = list(org_name_to_id.values())
    type_set = set(types)
    fetched: dict[str, list[dict]] = {t: [] for t in types}

    if "organizations" in type_set:
        orgs: list[dict] = []
        for name, oid in org_name_to_id.items():
            rows = await _list_resources_safe(
                target_client, "organizations", filters={"id": oid},
            )
            if not rows:
                rows = await _list_resources_safe(
                    target_client, "organizations", filters={"name": name},
                )
            orgs.extend(rows)
        fetched["organizations"] = _dedupe_by_id(orgs)

    child_types = type_set & INVENTORY_CHILD_TYPES
    need_schedules = "schedules" in type_set
    node_types = type_set & WORKFLOW_NODE_TYPES

    # Direct org-owned types (everything else that is not parent-scoped)
    org_owned = [
        t for t in types
        if t not in ORG_SCOPE_SKIP_TYPES
        and t != "organizations"
        and t not in INVENTORY_CHILD_TYPES
        and t != "schedules"
        and t not in WORKFLOW_NODE_TYPES
    ]
    for rtype in org_owned:
        fetched[rtype] = await _fetch_by_organization_ids(
            target_client, rtype, org_ids,
        )

    # Parent inventories for children / inventory_sources used by schedules
    need_inventory_parents = bool(child_types) or (
        need_schedules and "inventory_sources" not in fetched
    )
    if need_inventory_parents and not fetched.get("inventories"):
        invs = await _fetch_by_organization_ids(
            target_client, "inventories", org_ids,
        )
        if "inventories" in type_set:
            fetched["inventories"] = invs
        inventory_rows = invs
    else:
        inventory_rows = fetched.get("inventories", [])

    inv_ids = [
        tid for tid in (_object_target_id(o) for o in inventory_rows)
        if tid is not None
    ]

    for rtype in sorted(child_types):
        fetched[rtype] = await _fetch_children_by_parent_ids(
            target_client, rtype, "inventory", inv_ids,
        )

    if need_schedules:
        # Ensure schedule parents exist in fetched (may already from org_owned)
        for ptype in ("job_templates", "workflow_job_templates", "projects"):
            if not fetched.get(ptype):
                rows = await _fetch_by_organization_ids(
                    target_client, ptype, org_ids,
                )
                fetched[ptype] = rows
        if not fetched.get("inventory_sources"):
            rows = await _fetch_children_by_parent_ids(
                target_client, "inventory_sources", "inventory", inv_ids,
            )
            fetched["inventory_sources"] = rows

        parent_ids: list[int] = []
        for ptype in SCHEDULE_PARENT_TYPES:
            for obj in fetched.get(ptype, []):
                tid = _object_target_id(obj)
                if tid is not None:
                    parent_ids.append(tid)
        fetched["schedules"] = await _fetch_children_by_parent_ids(
            target_client, "schedules", "unified_job_template", parent_ids,
        )

    if node_types:
        if not fetched.get("workflow_job_templates"):
            rows = await _fetch_by_organization_ids(
                target_client, "workflow_job_templates", org_ids,
            )
            fetched["workflow_job_templates"] = rows
        nodes: list[dict] = []
        for wjt in fetched.get("workflow_job_templates", []):
            wid = _object_target_id(wjt)
            if wid is None:
                continue
            page = await _fetch_related_page(
                target_client, f"workflow_job_templates/{wid}/workflow_nodes/",
            )
            nodes.extend(page)
        nodes = _dedupe_by_id(nodes)
        for ntype in node_types:
            fetched[ntype] = nodes

    return {t: fetched.get(t, []) for t in types}


async def _fetch_live_all_types(
    target_client: Any,
    types: list[str],
) -> dict[str, list[dict]]:
    """Unscoped live fetch: list every type in full."""
    fetched: dict[str, list[dict]] = {}
    for rtype in types:
        fetched[rtype] = await _list_resources_safe(target_client, rtype)
    return fetched


async def _load_live_fk_reference_maps(
    target_client: Any,
) -> dict[str, dict[int, str]]:
    """Fetch pure-global types for FK id→name maps only (not validated)."""
    ref_objects: dict[str, list[dict]] = {}
    for rtype in FK_REFERENCE_TYPES:
        ref_objects[rtype] = await _list_resources_safe(target_client, rtype)
    return _build_id_to_name_maps(ref_objects)


async def fetch_live_target(
    target_client: Any,
    types: list[str],
    exports: dict[str, list[dict]],
    organizations: list[str] | None = None,
    fk_reference_exports: dict[str, list[dict]] | None = None,
) -> tuple[
    dict[str, dict],
    dict[str, dict[int, int]],
    dict[str, int],
    dict[str, list[int]],
    dict[str, list[dict]],
]:
    """Fetch live target objects and match them to exports by identity (name).

    When organizations is set (Plan B), fetch only those orgs via API filters
    and parent-scoped child lists — not the full controller then post-filter.
    Pure globals are not validated, but FK reference exports + a thin live
    global fetch keep credential_type / user / instance_group name maps for T3.

    Does not use migration DB id_mappings. Returns:
      field_data: {rtype: {c, s, t}} with target rows keyed by source id
      src_to_tgt: identity-matched source_id → target_id
      target_counts: live object count per type
      extra_target_ids: live target ids with no export identity match
      fetched: raw live objects per type (for T4 host sampling, etc.)
    """
    field_data: dict[str, dict] = {}
    src_to_tgt: dict[str, dict[int, int]] = {}
    target_counts: dict[str, int] = {}
    extra_target_ids: dict[str, list[int]] = {}
    total_start = time.monotonic()

    if organizations:
        logger.info(
            "validate_live_org_scoped_fetch",
            organizations=organizations,
            types=types,
        )
        fetched = await _fetch_live_org_scoped(
            target_client, types, organizations,
        )
    else:
        fetched = await _fetch_live_all_types(target_client, types)

    total_fetched = sum(len(v) for v in fetched.values())

    tgt_name_maps = _build_id_to_name_maps(fetched)
    src_name_maps = _build_id_to_name_maps(exports)
    # Do NOT merge source/target id→name maps: the same numeric id refers to
    # different objects on each controller (e.g. source NT 58 = alan-slack-alerts,
    # target NT 58 = sam-email-alerts). Resolve each side with its own map.

    if organizations:
        if fk_reference_exports:
            src_name_maps = _merge_id_to_name_maps(
                src_name_maps,
                _build_id_to_name_maps(fk_reference_exports),
            )
        live_fk_maps = await _load_live_fk_reference_maps(target_client)
        tgt_name_maps = _merge_id_to_name_maps(tgt_name_maps, live_fk_maps)
        logger.info(
            "validate_live_fk_reference_maps",
            source_types=sorted((fk_reference_exports or {}).keys()),
            target_types=sorted(live_fk_maps.keys()),
        )

    for rtype in types:
        if rtype not in fetched:
            fetched[rtype] = []
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
        org_scoped=bool(organizations),
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


def _extra_detail_parent_name(rtype: str, obj: dict) -> str:
    """Parent display name for extra-on-target detail rows."""
    if rtype in INVENTORY_CHILD_TYPES:
        return _parent_ref_name(obj, "inventory") or ""
    if rtype == "schedules":
        return _parent_ref_name(obj, "unified_job_template") or ""
    if rtype in WORKFLOW_NODE_TYPES:
        return _parent_ref_name(obj, "workflow_job_template") or ""
    return ""


def _build_extra_details(
    rtype: str,
    extra_ids: list[int],
    target_objects: list[dict],
    inv_org_by_name: dict[str, tuple[str, Optional[int]]],
    inv_org_by_id: dict[int, tuple[str, Optional[int]]],
    ujt_org_by_name: dict[str, tuple[str, Optional[int]]],
    *,
    max_details: int = EXTRA_DETAILS_MAX_PER_TYPE,
) -> tuple[list[ExtraDetail], bool, int]:
    """Build ExtraDetail rows for target IDs with no export identity match.

    Returns (details, truncated, omitted_count).
    """
    if not extra_ids or rtype in EXTRA_TAB_EXCLUDE_TYPES:
        return [], False, 0

    by_id: dict[int, dict] = {}
    for obj in target_objects:
        tid = _object_target_id(obj)
        if tid is not None and tid not in by_id:
            by_id[tid] = obj

    truncated = len(extra_ids) > max_details
    omitted = max(0, len(extra_ids) - max_details) if truncated else 0
    details: list[ExtraDetail] = []
    for tid in extra_ids[:max_details]:
        obj = by_id.get(tid)
        if obj is None:
            details.append(ExtraDetail(
                name=f"id:{tid}",
                organization="",
                parent_type=rtype,
                target_id=tid,
            ))
            continue
        name = _object_display_name(obj) or f"id:{tid}"
        org_name, _ = _resolve_object_org(
            rtype, obj, inv_org_by_name, inv_org_by_id, ujt_org_by_name,
        )
        details.append(ExtraDetail(
            name=name,
            organization=org_name,
            parent_type=rtype,
            parent_name=_extra_detail_parent_name(rtype, obj),
            target_id=tid,
        ))
    return details, truncated, omitted


_SYNC_ENTRY_TYPES = ("projects", "inventory_sources")


def _build_sync_entries(
    live_fetched: dict[str, list[dict]],
    inv_org_by_name: dict[str, tuple[str, Optional[int]]],
    inv_org_by_id: dict[int, tuple[str, Optional[int]]],
    ujt_org_by_name: dict[str, tuple[str, Optional[int]]],
) -> list[SyncEntry]:
    """Build sync status rows for all live target projects and inventory sources."""
    entries: list[SyncEntry] = []
    for rtype in _SYNC_ENTRY_TYPES:
        for obj in live_fetched.get(rtype, []):
            sync_status, failed, job_id = classify_sync_from_api(obj)
            tid = _object_target_id(obj)
            name = _object_display_name(obj) or (f"id:{tid}" if tid is not None else "unknown")
            org_name, _ = _resolve_object_org(
                rtype, obj, inv_org_by_name, inv_org_by_id, ujt_org_by_name,
            )
            entries.append(SyncEntry(
                name=name,
                resource_type=rtype,
                organization=org_name,
                target_id=tid,
                sync_status=sync_status,
                failed=failed,
                last_job_id=job_id,
            ))
    return entries


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
    live_fetched: dict[str, list[dict]] | None = None,
    auditor_cross_check: AuditorCrossCheck | None = None,
    t4_host_sampling: T4HostSampling | None = None,
    organizations: list[str] | None = None,
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
        organizations=list(organizations or []),
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

    # Target-side org maps for Extra-on-target detail resolution (live only)
    tgt_inv_org_by_name: dict[str, tuple[str, Optional[int]]] = {}
    tgt_inv_org_by_id: dict[int, tuple[str, Optional[int]]] = {}
    tgt_ujt_org_by_name: dict[str, tuple[str, Optional[int]]] = {}
    if live_mode and live_fetched:
        tgt_inv_org_by_name, tgt_inv_org_by_id = _build_inventory_org_maps(
            live_fetched,
        )
        tgt_ujt_org_by_name = _build_ujt_org_map(
            live_fetched, tgt_inv_org_by_name, tgt_inv_org_by_id,
        )

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

    # Organization name → (source_id, target_id) for Org Health src/tgt display
    org_id_by_name: dict[str, tuple[Optional[int], Optional[int]]] = {}
    org_src_to_tgt = src_to_tgt.get("organizations", {})
    for org_obj in exports.get("organizations", []):
        oname = _object_display_name(org_obj)
        osid = _object_source_id(org_obj)
        if not oname or osid is None:
            continue
        org_id_by_name[oname] = (osid, org_src_to_tgt.get(osid))

    for rtype in types:
        stats = all_stats.get(rtype, {})
        mapping = src_to_tgt.get(rtype, {})
        src_objects = exports.get(rtype, [])
        src_ids = [
            sid for o in src_objects
            if (sid := _object_source_id(o)) is not None
        ]
        src_count = len(src_ids)
        org_scoped = bool(organizations)

        if org_scoped:
            # Matched/missing must be relative to filtered exports — not the
            # full platform id_map / migration_stats totals.
            matched = sum(1 for sid in src_ids if sid in mapping)
        else:
            matched = len(mapping)
        missing = max(0, src_count - matched)
        if live_mode:
            tgt_count = live_target_counts.get(rtype, 0)
            extra_count = len((live_extra_ids or {}).get(rtype, []))
        elif org_scoped:
            # Inventory is already filtered to scoped export source IDs.
            tgt_count = sum(
                1
                for row in obj_inventory.get(rtype, [])
                if len(row) >= 3 and row[2] in ("completed", "skipped")
            )
            extra_count = 0
        else:
            # Target = successfully present on AAP (imported or skipped-as-existing)
            tgt_count = stats.get("completed", 0) + stats.get("skipped", 0)
            extra_count = 0

        # Build missing_details and per-org counts in one pass over exports.
        missing_details: list[MissingDetail] = []
        explained_failures = 0
        explained_skips = 0
        unexplained = 0
        for obj in src_objects:
            sid = _object_source_id(obj)
            if sid is None:
                continue
            org_info = obj_to_org.get((rtype, sid))
            org_name = org_info[0] if org_info else ""
            org_key = (
                org_info[0] if org_info
                else (GLOBAL_ORG if rtype in UNSCOPED_TYPES else "")
            )

            if sid not in mapping:
                obj_name = _object_display_name(obj) or str(sid)
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

            if not org_key:
                continue

            if org_key not in per_org_data:
                org_id = org_info[1] if org_info else None
                mapped = org_id_by_name.get(org_key)
                if mapped:
                    src_org_id, tgt_org_id = mapped
                else:
                    src_org_id, tgt_org_id = org_id, None
                per_org_data[org_key] = OrgValidationSummary(
                    org_name=org_key,
                    source_id=src_org_id if src_org_id is not None else org_id,
                    target_id=tgt_org_id,
                )
                org_type_counts[org_key] = {}

            org_summary = per_org_data[org_key]
            org_summary.total_objects += 1

            is_matched = sid in mapping
            if is_matched:
                org_summary.matched += 1
            else:
                org_summary.missing += 1

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

        extra_details: list[ExtraDetail] = []
        extra_truncated = False
        extra_omitted = 0
        if live_mode and live_extra_ids is not None:
            extra_details, extra_truncated, extra_omitted = _build_extra_details(
                rtype,
                live_extra_ids.get(rtype, []),
                (live_fetched or {}).get(rtype, []),
                tgt_inv_org_by_name,
                tgt_inv_org_by_id,
                tgt_ujt_org_by_name,
            )

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
                extra_details=extra_details,
                extra_truncated=extra_truncated,
                extra_truncated_count=extra_omitted,
            ),
            t3_field_parity=t3,
        ))

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
        explained_failures, explained_skips = count_explained_gaps(
            [md.explanation or "" for md in org_summary.missing_details],
        )
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
                    GLOBAL_ORG if rtype in UNSCOPED_TYPES else ""
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
                    GLOBAL_ORG if rtype in UNSCOPED_TYPES else ""
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
                    GLOBAL_ORG if rtype in UNSCOPED_TYPES else ""
                )
                entries.append(ObjectEntry(
                    name=obj_name,
                    organization=org_name,
                    source_id=sid,
                    status="pending",
                    error="Not tracked in migration DB",
                ))
        object_inventory[rtype] = entries

    # Mark inventory entries that have field differences (status stays completed/etc.)
    changed_sids: dict[str, set[int]] = defaultdict(set)
    for rtype, findings in per_type_findings.items():
        for ff in findings:
            if ff.source_id is not None:
                changed_sids[rtype].add(ff.source_id)
    for rtype, entries in object_inventory.items():
        sid_set = changed_sids.get(rtype)
        if not sid_set:
            continue
        for entry in entries:
            if entry.source_id is not None and entry.source_id in sid_set:
                entry.field_changed = True

    sync_entries: list[SyncEntry] = []
    if live_mode and live_fetched:
        sync_entries = _build_sync_entries(
            live_fetched,
            tgt_inv_org_by_name,
            tgt_inv_org_by_id,
            tgt_ujt_org_by_name,
        )

    sync_failed = sum(1 for entry in sync_entries if entry.failed)

    per_type_results = apply_migration_buckets(per_type_results, object_inventory)

    return ValidationResult(
        metadata=meta,
        executive_summary=build_executive_summary(
            per_type_results,
            sync_failed=sync_failed,
        ),
        per_type=per_type_results,
        per_org=per_org_data,
        object_inventory=object_inventory,
        t4_host_sampling=t4,
        auditor_cross_check=auditor_cross_check or AuditorCrossCheck(),
        sync_entries=sync_entries,
    )


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

# Export artifacts that are not AAP listable resource collections
_NON_RESOURCE_EXPORT_KEYS = {"metadata", "settings"}


def _types_with_export_objects(
    types: list[str],
    exports: dict[str, list[dict]],
) -> list[str]:
    """Keep only types that still have export objects after --orgs filter."""
    filtered = [t for t in types if exports.get(t)]
    if not filtered:
        raise ValueError(
            "No export objects remain after applying --orgs filter"
        )
    return filtered


async def run_validation(
    config: Any,
    migration_state: Any | None = None,
    target_client: Any | None = None,
    live: bool = False,
    resource_type: str | None = None,
    skip_hosts: bool = False,
    organizations: list[str] | None = None,
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
        organizations: When set, scope validation to these org names.
            Filters export objects; live mode fetches only those orgs via
            API filters (Plan B). Skips auditor and pure global types.
    """
    export_dir = Path(config.paths.export_dir)
    logger.info(
        "validate_start",
        export_dir=str(export_dir),
        live=live,
        skip_hosts=skip_hosts,
        organizations=organizations or [],
    )

    skip_types = {"hosts"} if skip_hosts else None
    exports = load_exports(export_dir, skip_types=skip_types)
    # Drop non-resource export files (metadata.json, settings blob, …)
    for key in list(exports.keys()):
        if key in _NON_RESOURCE_EXPORT_KEYS:
            exports.pop(key, None)

    if skip_hosts:
        logger.info("validate_skip_hosts")

    fk_reference_exports: dict[str, list[dict]] = {}
    if organizations:
        # Keep globals for FK name maps (live T3) before org filter drops them
        fk_reference_exports = extract_fk_reference_exports(exports)
        before = sum(len(v) for v in exports.values())
        exports = filter_exports_by_orgs(exports, organizations)
        after = sum(len(v) for v in exports.values())
        logger.info(
            "validate_org_scope_applied",
            organizations=organizations,
            objects_before=before,
            objects_after=after,
            fk_reference_types=sorted(fk_reference_exports.keys()),
        )

    logger.info("validate_exports_loaded", types=len(exports),
                total_objects=sum(len(v) for v in exports.values()))

    if live:
        if target_client is None:
            raise ValueError("--live requires target API access (target_client)")

        base_url = getattr(target_client, "base_url", "target AAP")
        try:
            await target_client.get("ping/")
        except (NetworkError, ServerError) as exc:
            raise ValueError(
                f"Target AAP is not available ({base_url}). "
                f"Could not reach GET /api/controller/v2/ping/: {exc}"
            ) from exc
        logger.info("validate_target_ping_ok", target_url=base_url)

        types = sorted(exports.keys())
        if resource_type:
            if resource_type not in types:
                raise ValueError(f"Resource type '{resource_type}' not found in exports")
            types = [resource_type]
        elif skip_hosts:
            types = [t for t in types if t != "hosts"]
        elif organizations:
            types = _types_with_export_objects(types, exports)

        field_data, src_to_tgt, target_counts, extra_ids, fetched = await fetch_live_target(
            target_client,
            types,
            exports,
            organizations=organizations,
            fk_reference_exports=fk_reference_exports or None,
        )

        # Import DB explains unmatched objects (failed/skipped vs unexplained)
        all_stats: dict[str, dict] = {}
        obj_inventory: dict[str, list] = {}
        if migration_state is not None:
            for rtype in types:
                all_stats[rtype] = migration_state.get_migration_stats(rtype)
            obj_inventory = _query_object_inventory(migration_state.database_url, types)
            if organizations:
                obj_inventory = _filter_obj_inventory_to_exports(obj_inventory, exports)
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

        # Org-scoped runs skip auditor (platform-wide); full live still runs it
        auditor_check: AuditorCrossCheck | None = None
        if organizations:
            logger.info(
                "validate_auditor_skipped",
                reason="org-scoped validation",
                organizations=organizations,
            )
        else:
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
            live_fetched=fetched,
            auditor_cross_check=auditor_check,
            t4_host_sampling=t4,
            organizations=organizations,
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

        if organizations and not resource_type:
            types = _types_with_export_objects(types, exports)

        src_to_tgt = build_id_maps(migration_state, types)

        all_stats: dict[str, dict] = {}
        for rtype in types:
            all_stats[rtype] = migration_state.get_migration_stats(rtype)

        obj_inventory = _query_object_inventory(migration_state.database_url, types)
        if organizations:
            obj_inventory = _filter_obj_inventory_to_exports(obj_inventory, exports)
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
            organizations=organizations,
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
