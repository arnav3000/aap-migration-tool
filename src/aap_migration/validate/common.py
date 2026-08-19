"""Shared validation helpers and type-group constants."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal, Optional

from aap_migration.validate.models import (
    ExecutiveSummary,
    MissingDetail,
    ObjectEntry,
    PerTypeResult,
)

# Platform-wide types with no organization (includes organizations for inventory).
UNSCOPED_TYPES = frozenset({
    "users", "organizations", "credential_types", "instance_groups",
    "instances", "settings",
})

# Pure globals omitted from --orgs scoped validation (organizations kept).
ORG_SCOPE_SKIP_TYPES = frozenset({
    "users", "credential_types", "instance_groups", "instances", "settings",
})

# Export types fetched via inventory parent (not organization=).
INVENTORY_CHILD_TYPES = frozenset({
    "hosts", "inventory_groups", "inventory_sources",
})

WORKFLOW_NODE_TYPES = frozenset({
    "workflow_nodes", "workflow_job_template_nodes",
})

SCHEDULE_PARENT_TYPES = (
    "job_templates", "workflow_job_templates", "projects", "inventory_sources",
)

# Globals loaded only for FK id→name maps under --orgs (not validated).
FK_REFERENCE_TYPES = frozenset(ORG_SCOPE_SKIP_TYPES - {"settings"})

ExplanationBucket = Literal["failed", "skipped", "other"]

SYNC_FAILED_STATUSES = frozenset({"failed", "error", "canceled"})


def classify_sync_from_api(obj: dict) -> tuple[str, bool, Optional[int]]:
    """Derive sync status, failure flag, and last update job id from a live API object."""
    summary = obj.get("summary_fields") or {}
    last_update = summary.get("last_update") or {}
    if not isinstance(last_update, dict):
        last_update = {}

    job_id = last_update.get("id")
    if job_id is not None:
        try:
            job_id = int(job_id)
        except (TypeError, ValueError):
            job_id = None

    lu_status = str(last_update.get("status") or "").strip().lower()
    lu_failed = bool(last_update.get("failed"))
    top_status = str(obj.get("status") or "").strip().lower()
    last_update_failed = bool(obj.get("last_update_failed"))

    if lu_status:
        display_status = lu_status
    elif top_status:
        display_status = top_status
    else:
        display_status = "never updated"

    is_failed = (
        last_update_failed
        or lu_failed
        or lu_status in SYNC_FAILED_STATUSES
        or (not lu_status and top_status in SYNC_FAILED_STATUSES)
    )
    return display_status, is_failed, job_id


def classify_explanation(explanation: str) -> ExplanationBucket:
    """Classify a missing-object explanation for gap accounting."""
    text = explanation or ""
    if text.startswith("Failed") or "(Failed" in text:
        return "failed"
    if text.startswith("Skipped") or "(Skipped" in text:
        return "skipped"
    return "other"


def count_explained_gaps(
    explanations: list[str],
) -> tuple[int, int]:
    """Return (explained_failures, explained_skips) from explanation strings."""
    failures = 0
    skips = 0
    for explanation in explanations:
        bucket = classify_explanation(explanation)
        if bucket == "failed":
            failures += 1
        elif bucket == "skipped":
            skips += 1
    return failures, skips


def count_explained_from_missing(
    missing_details: list[MissingDetail],
) -> tuple[int, int, int]:
    """Return (explained_failures, explained_skips, unexplained)."""
    explanations = [md.explanation or "" for md in missing_details]
    failures, skips = count_explained_gaps(explanations)
    unexplained = max(0, len(missing_details) - failures - skips)
    return failures, skips, unexplained


def type_migration_bucket(
    per_type: PerTypeResult,
    inventory: list[ObjectEntry] | None = None,
) -> Literal["c", "f", "s", "p"]:
    """Classify a resource type for T1 summary cards (mirrors report.js typeMigrationBucket)."""
    items = inventory or []
    counts = per_type.t1_counts
    existence = per_type.t2_existence

    if (
        counts.explained_failures > 0
        or counts.unexplained > 0
        or existence.missing_on_target > 0
    ):
        return "f"

    for item in items:
        status = item.status[0] if item.status else ""
        if status == "f":
            return "f"

    if not items:
        return "f" if existence.missing_on_target > 0 else "p"

    has_skipped = False
    has_pending = False
    for item in items:
        status = item.status[0] if item.status else ""
        if status == "s":
            has_skipped = True
        elif status == "p":
            has_pending = True

    if has_skipped:
        return "s"
    if has_pending:
        return "p"
    return "c"


def apply_migration_buckets(
    per_type: list[PerTypeResult],
    object_inventory: dict[str, list[ObjectEntry]],
) -> list[PerTypeResult]:
    """Set migration_bucket on each per-type row from counts + inventory."""
    return [
        replace(
            t,
            migration_bucket=type_migration_bucket(
                t, object_inventory.get(t.resource_type, [])
            ),
        )
        for t in per_type
    ]


def count_failed_resource_types(per_type: list[PerTypeResult]) -> int:
    """Count resource types in the failed migration bucket."""
    return sum(1 for t in per_type if t.migration_bucket == "f")


def build_executive_summary(
    per_type: list[PerTypeResult],
    *,
    sync_failed: int = 0,
) -> ExecutiveSummary:
    """Build executive summary totals from per-type results."""
    total_missing = sum(t.t2_existence.missing_on_target for t in per_type)
    total_extra = sum(t.t2_existence.extra_on_target for t in per_type)
    total_field_mm = sum(t.t3_field_parity.mismatching for t in per_type)
    total_explained = sum(
        t.t1_counts.explained_failures + t.t1_counts.explained_skips
        for t in per_type
    )
    total_unexplained = sum(t.t1_counts.unexplained for t in per_type)
    types_with_unexplained = sum(
        1 for t in per_type if t.t1_counts.unexplained > 0
    )
    verdict = (
        "PASS"
        if total_unexplained == 0
        and total_field_mm == 0
        and sync_failed == 0
        else "REVIEW REQUIRED"
    )
    return ExecutiveSummary(
        total_resource_types=len(per_type),
        types_with_unexplained_delta=types_with_unexplained,
        total_missing_on_target=total_missing,
        total_extra_on_target=total_extra,
        total_field_mismatches=total_field_mm,
        total_explained=total_explained,
        total_sync_failed=sync_failed,
        verdict=verdict,
    )


def int_dict_key(value: Any) -> Optional[int]:
    """Coerce a field-data dict key to int when possible."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def item_org_name(item: Any, attr: str = "organization") -> str:
    """Normalized organization name from a detail/finding object."""
    return getattr(item, attr, None) or ""


def belongs_to_org(item: Any, org_name: str, attr: str = "organization") -> bool:
    """Whether an item belongs to the given organization."""
    return item_org_name(item, attr) == org_name
