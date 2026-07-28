"""Data models for post-migration validation results.

Every finding carries name, org (or parent), source_id, target_id as
DISTINCT fields. Display composes them; nothing is pre-concatenated.
JSON stays machine-consumable for customer scripting/ticketing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ExclusionSets:
    metadata_fields: int = 0
    computed_fields: int = 0
    related_collections: int = 0
    fk_fields_by_name: int = 0
    version_gap_defaults: int = 0
    type_specific_overrides: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata_fields": self.metadata_fields,
            "computed_fields": self.computed_fields,
            "related_collections": self.related_collections,
            "fk_fields_by_name": self.fk_fields_by_name,
            "version_gap_defaults": self.version_gap_defaults,
            "type_specific_overrides": self.type_specific_overrides,
        }


@dataclass
class ValidationMetadata:
    run_id: str = ""
    mode: str = "live"
    started_at: str = ""
    completed_at: str = ""
    source_url: str = ""
    target_url: str = ""
    tiers_run: list[str] = field(default_factory=list)
    host_sample_size: int = 0
    host_sample_seed: int = 0
    read_only: bool = True
    total_api_calls: int = 0
    comparison_rules_version: str = "1.0"
    exclusion_sets: ExclusionSets = field(default_factory=ExclusionSets)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "source_url": self.source_url,
            "target_url": self.target_url,
            "tiers_run": self.tiers_run,
            "host_sample_size": self.host_sample_size,
            "host_sample_seed": self.host_sample_seed,
            "read_only": self.read_only,
            "total_api_calls": self.total_api_calls,
            "comparison_rules_version": self.comparison_rules_version,
            "exclusion_sets": self.exclusion_sets.to_dict(),
        }


@dataclass
class ExecutiveSummary:
    total_resource_types: int = 0
    types_with_unexplained_delta: int = 0
    total_missing_on_target: int = 0
    total_extra_on_target: int = 0
    total_field_mismatches: int = 0
    total_explained: int = 0
    verdict: str = "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_resource_types": self.total_resource_types,
            "types_with_unexplained_delta": self.types_with_unexplained_delta,
            "total_missing_on_target": self.total_missing_on_target,
            "total_extra_on_target": self.total_extra_on_target,
            "total_field_mismatches": self.total_field_mismatches,
            "total_explained": self.total_explained,
            "verdict": self.verdict,
        }


@dataclass
class T1Counts:
    source: int = 0
    target: int = 0
    delta: int = 0
    explained_failures: int = 0
    explained_skips: int = 0
    unexplained: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "source": self.source,
            "target": self.target,
            "delta": self.delta,
            "explained_failures": self.explained_failures,
            "explained_skips": self.explained_skips,
            "unexplained": self.unexplained,
        }


@dataclass
class MissingDetail:
    name: str = ""
    organization: str = ""
    parent_type: str = ""
    parent_name: str = ""
    source_id: Optional[int] = None
    target_id: Optional[int] = None
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "organization": self.organization,
            "parent_type": self.parent_type,
            "parent_name": self.parent_name,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "explanation": self.explanation,
        }


@dataclass
class T2Existence:
    matched: int = 0
    missing_on_target: int = 0
    extra_on_target: int = 0
    missing_details: list[MissingDetail] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "missing_on_target": self.missing_on_target,
            "extra_on_target": self.extra_on_target,
            "missing_details": [d.to_dict() for d in self.missing_details],
        }


@dataclass
class FieldFinding:
    name: str = ""
    organization: str = ""
    parent_type: str = ""
    parent_name: str = ""
    source_id: Optional[int] = None
    target_id: Optional[int] = None
    field: str = ""
    source_value: str = ""
    target_value: str = ""
    tier: str = "T3"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "organization": self.organization,
            "parent_type": self.parent_type,
            "parent_name": self.parent_name,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "field": self.field,
            "source_value": self.source_value,
            "target_value": self.target_value,
            "tier": self.tier,
        }


@dataclass
class T3FieldParity:
    compared: int = 0
    matching: int = 0
    mismatching: int = 0
    findings: list[FieldFinding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "compared": self.compared,
            "matching": self.matching,
            "mismatching": self.mismatching,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class PerTypeResult:
    resource_type: str = ""
    display_name: str = ""
    t1_counts: T1Counts = field(default_factory=T1Counts)
    t2_existence: T2Existence = field(default_factory=T2Existence)
    t3_field_parity: T3FieldParity = field(default_factory=T3FieldParity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "display_name": self.display_name,
            "t1_counts": self.t1_counts.to_dict(),
            "t2_existence": self.t2_existence.to_dict(),
            "t3_field_parity": self.t3_field_parity.to_dict(),
        }


@dataclass
class InventoryCountDetail:
    inventory: str = ""
    source_id: Optional[int] = None
    target_id: Optional[int] = None
    source_count: int = 0
    target_count: int = 0
    delta: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "inventory": self.inventory,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "source_count": self.source_count,
            "target_count": self.target_count,
            "delta": self.delta,
        }


@dataclass
class PerInventoryCountParity:
    matching: int = 0
    mismatching: int = 0
    details: list[InventoryCountDetail] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "matching": self.matching,
            "mismatching": self.mismatching,
            "details": [d.to_dict() for d in self.details],
        }


@dataclass
class T4HostSampling:
    total_hosts_source: int = 0
    total_hosts_target: int = 0
    inventories_checked: int = 0
    sample_size: int = 0
    sample_methodology: str = "stratified by inventory, fixed-seed reproducible"
    confidence: str = "99%"
    margin_of_error: str = "4.5%"
    field_mismatches_in_sample: int = 0
    per_inventory_count_parity: PerInventoryCountParity = field(
        default_factory=PerInventoryCountParity
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_hosts_source": self.total_hosts_source,
            "total_hosts_target": self.total_hosts_target,
            "inventories_checked": self.inventories_checked,
            "sample_size": self.sample_size,
            "sample_methodology": self.sample_methodology,
            "confidence": self.confidence,
            "margin_of_error": self.margin_of_error,
            "field_mismatches_in_sample": self.field_mismatches_in_sample,
            "per_inventory_count_parity": self.per_inventory_count_parity.to_dict(),
        }


@dataclass
class AuditorDetail:
    username: str = ""
    source_id: Optional[int] = None
    target_id: Optional[int] = None
    source_is_system_auditor: bool = False
    gateway_has_platform_auditor: bool = False
    match: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "source_is_system_auditor": self.source_is_system_auditor,
            "gateway_has_platform_auditor": self.gateway_has_platform_auditor,
            "match": self.match,
        }


@dataclass
class AuditorCrossCheck:
    source_system_auditors: int = 0
    gateway_platform_auditors: int = 0
    mismatches: int = 0
    details: list[AuditorDetail] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_system_auditors": self.source_system_auditors,
            "gateway_platform_auditors": self.gateway_platform_auditors,
            "mismatches": self.mismatches,
            "details": [d.to_dict() for d in self.details],
        }


@dataclass
class ObjectEntry:
    """Single object in the migration inventory."""

    name: str = ""
    organization: str = ""
    source_id: Optional[int] = None
    target_id: Optional[int] = None
    status: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"n": self.name, "s": self.source_id, "st": self.status[0] if self.status else ""}
        if self.organization:
            d["o"] = self.organization
        if self.target_id is not None:
            d["t"] = self.target_id
        if self.error:
            d["e"] = self.error
        return d


@dataclass
class OrgTypeRollup:
    resource_type: str = ""
    source: int = 0
    target: int = 0
    matched: int = 0
    missing: int = 0
    extra: int = 0
    field_mismatches: int = 0
    unexplained: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "source": self.source,
            "target": self.target,
            "matched": self.matched,
            "missing": self.missing,
            "extra": self.extra,
            "field_mismatches": self.field_mismatches,
            "unexplained": self.unexplained,
        }


@dataclass
class OrgValidationSummary:
    org_name: str = ""
    source_id: Optional[int] = None
    target_id: Optional[int] = None
    total_objects: int = 0
    matched: int = 0
    missing: int = 0
    extra: int = 0
    field_mismatches: int = 0
    unexplained: int = 0
    per_type: list[OrgTypeRollup] = field(default_factory=list)
    missing_details: list[MissingDetail] = field(default_factory=list)
    field_findings: list[FieldFinding] = field(default_factory=list)

    @property
    def health(self) -> str:
        if self.unexplained > 0:
            return "red"
        if self.missing > 0 or self.field_mismatches > 0:
            return "amber"
        return "green"

    def to_dict(self) -> dict[str, Any]:
        return {
            "org_name": self.org_name,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "total_objects": self.total_objects,
            "matched": self.matched,
            "missing": self.missing,
            "extra": self.extra,
            "field_mismatches": self.field_mismatches,
            "unexplained": self.unexplained,
            "health": self.health,
            "per_type": [t.to_dict() for t in self.per_type],
            "missing_details": [d.to_dict() for d in self.missing_details],
            "field_findings": [f.to_dict() for f in self.field_findings],
        }


@dataclass
class ValidationResult:
    """Top-level validation result."""

    metadata: ValidationMetadata = field(default_factory=ValidationMetadata)
    executive_summary: ExecutiveSummary = field(default_factory=ExecutiveSummary)
    per_type: list[PerTypeResult] = field(default_factory=list)
    per_org: dict[str, OrgValidationSummary] = field(default_factory=dict)
    object_inventory: dict[str, list[ObjectEntry]] = field(default_factory=dict)
    t4_host_sampling: T4HostSampling = field(default_factory=T4HostSampling)
    auditor_cross_check: AuditorCrossCheck = field(
        default_factory=AuditorCrossCheck
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "executive_summary": self.executive_summary.to_dict(),
            "per_type": [t.to_dict() for t in self.per_type],
            "per_org": {k: v.to_dict() for k, v in self.per_org.items()},
            "t4_host_sampling": self.t4_host_sampling.to_dict(),
            "auditor_cross_check": self.auditor_cross_check.to_dict(),
        }

    def inventory_to_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            rtype: [e.to_dict() for e in entries]
            for rtype, entries in self.object_inventory.items()
        }
