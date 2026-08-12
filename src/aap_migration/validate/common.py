"""Shared validation helpers and type-group constants."""

from __future__ import annotations

from typing import Any, Literal, Optional

from aap_migration.validate.models import ExecutiveSummary, MissingDetail, PerTypeResult

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


def build_executive_summary(per_type: list[PerTypeResult]) -> ExecutiveSummary:
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
        if total_unexplained == 0 and total_field_mm == 0
        else "REVIEW REQUIRED"
    )
    return ExecutiveSummary(
        total_resource_types=len(per_type),
        types_with_unexplained_delta=types_with_unexplained,
        total_missing_on_target=total_missing,
        total_extra_on_target=total_extra,
        total_field_mismatches=total_field_mm,
        total_explained=total_explained,
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
