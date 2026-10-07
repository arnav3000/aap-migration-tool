"""ETL / IAM / validation / reporting / state request schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ._shared import ChainedRequest, ConnectionSelector, parse_organizations

# -- migrate / ETL --------------------------------------------------------
# Single shared phase type (superset of migrate + import CLI choices).
# Per-command validation still rejects phases its CLI command does not
# support (see MigrateRequest validator: migrate has no phase3).
MigrationPhase = Literal["phase1", "phase2", "phase3", "all"]


def _normalize_phase_value(value: Any) -> Any:
    """Lowercase a phase string (CLI Choice is case_insensitive=False-safe)."""
    if isinstance(value, str):
        return value.lower()
    return value


def _lowercase_phase(data: Any) -> Any:
    """Shared before-validator: lowercase ``phase`` when present."""
    if isinstance(data, dict) and "phase" in data:
        data = dict(data)
        data["phase"] = _normalize_phase_value(data["phase"])
    return data


class MigrateRequest(ChainedRequest):
    resource_types: list[str] | None = Field(
        default=None,
        description="None (omitted) means all resource types; explicit [] is a no-op selecting none.",
    )
    force: bool = False
    resume: bool = False
    skip_prep: bool = False
    phase: MigrationPhase = "all"

    @model_validator(mode="before")
    @classmethod
    def _normalize_phase(cls, data: Any) -> Any:
        return _lowercase_phase(data)

    @model_validator(mode="after")
    def _check_phase(self) -> MigrateRequest:
        # Migrate CLI accepts only phase1/phase2/all (no phase3); import
        # accepts the full superset. Reject phase3 here with 422.
        if self.phase == "phase3":
            raise ValueError("phase3 is not supported for migrate; use phase1, phase2, or all")
        return self


class MigrateResumeRequest(ChainedRequest):
    from_phase: str | None = None
    disable_progress: bool = False
    quiet: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize_from_phase(cls, data: Any) -> Any:
        if isinstance(data, dict) and "from_phase" in data:
            data = dict(data)
            if data["from_phase"] is not None:
                data["from_phase"] = _normalize_phase_value(data["from_phase"])
        return data

    @model_validator(mode="after")
    def _check_from_phase(self) -> MigrateResumeRequest:
        # Value-domain rejection lives in the schema so it is 422 by
        # construction (same dialect as automatic validation), not a
        # hand-raised router 422 beside 400/409 siblings.
        # Domain matches CLI MIGRATION_PHASES, which is ALL_RESOURCE_TYPES
        # (cli/commands/migrate.py); same set, same canonicalization.
        if self.from_phase is not None:
            from aap_migration.resources import ALL_RESOURCE_TYPES

            lowered = {str(p).lower(): str(p) for p in ALL_RESOURCE_TYPES}
            canonical = lowered.get(str(self.from_phase).lower())
            if canonical is None:
                raise ValueError(f"Unknown phase '{self.from_phase}'")
            object.__setattr__(self, "from_phase", canonical)
        return self


class ExportRequest(ChainedRequest):
    resource_types: list[str] | None = Field(
        default=None,
        description="None (omitted) means all resource types; explicit [] is a no-op selecting none.",
    )
    force: bool = False
    # Unbounded like the CLI (--records-per-file is a plain int): the API
    # adds no floor/ceiling so CLI-valid values never 422 here.
    records_per_file: int | None = Field(default=None)
    resume: bool = False


class TransformRequest(ChainedRequest):
    resource_types: list[str] | None = Field(
        default=None,
        description="None (omitted) means all resource types; explicit [] is a no-op selecting none.",
    )
    force: bool = False
    quiet: bool = False
    disable_progress: bool = False
    skip_pending_deletion: bool = True
    defer_project_sync: bool = True


class ImportRequest(ChainedRequest):
    resource_types: list[str] | None = Field(
        default=None,
        description="None (omitted) means all resource types; explicit [] is a no-op selecting none.",
    )
    force: bool = False
    resume: bool = False
    dry_run: bool = False
    skip_dependencies: bool = False
    check_dependencies: bool = False
    force_reimport: bool = False
    phase: MigrationPhase = "all"

    @model_validator(mode="before")
    @classmethod
    def _normalize_phase(cls, data: Any) -> Any:
        return _lowercase_phase(data)


class PatchProjectsRequest(ChainedRequest):
    batch_size: int | None = Field(default=None, ge=1)
    interval: int | None = Field(default=None, ge=0)


class ImportDependencyCheckRequest(BaseModel):
    """Narrow schema for POST /imports/check-dependencies (no mutation flags).

    Rejects dry_run/force/phase and other ImportRequest mutation fields with
    422 (extra=forbid) instead of silently ignoring them.
    """

    model_config = {"extra": "forbid"}

    job_id: str | None = Field(default=None)
    source_id: str | None = Field(default=None)
    target_id: str | None = Field(default=None)
    resource_types: list[str] | None = Field(default=None)
    allow_pair_switch: bool = False


class GranularImportRequest(ChainedRequest):
    steps: list[str] | None = Field(
        default=None,
        description=(
            "Micro-phase steps in order. None (omitted) means full "
            "MICRO_PHASES order; explicit [] is a no-op selecting none, "
            "consistent with every resource_types field. "
            "Example: ['organizations', 'users', 'projects']."
        ),
    )
    dry_run: bool = False


# -- credentials -----------------------------------------------------------
class CredentialCompareRequest(ChainedRequest):
    pass


class CredentialMigrateRequest(ChainedRequest):
    dry_run: bool = False


class CredentialReportRequest(ChainedRequest):
    pass


# -- IAM -------------------------------------------------------------------
class IamAuditRequest(ChainedRequest):
    """IAM audit request. Supports ``job_id`` chaining so ``resume=true``
    reuses the referenced job's working directory and checkpoint file
    (``iam_reports/iam_checkpoint.json``); without ``job_id``, resume has
    no checkpoint to continue from."""

    verify_ssl: bool | None = Field(
        default=None,
        description="Override stored connection verify_ssl (default: stored value). "
        "A false override never weakens a stored verify_ssl=true connection "
        "(rejected with 400; update the stored connection instead).",
    )
    timeout: int | None = Field(
        default=None, ge=1, le=1200, description="Override stored timeout (default: stored value)"
    )
    workers: int = Field(default=1, ge=1, le=64)
    scan_strategy: Literal["resource", "principal"] = "resource"
    resume: bool = False


class IamMigrateRequest(IamAuditRequest):
    dry_run: bool = False
    skip_user_roles: bool = False
    users_only: bool = False

    @model_validator(mode="after")
    def _check_exclusive(self) -> IamMigrateRequest:
        if self.skip_user_roles and self.users_only:
            raise ValueError("skip_user_roles and users_only are mutually exclusive")
        return self


class IamBenchmarkRequest(BaseModel):
    model_config = {"extra": "forbid"}

    source_id: str | None = None
    verify_ssl: bool | None = Field(
        default=None,
        description="Override stored verify_ssl. A false override never "
        "weakens a stored verify_ssl=true connection (rejected with 400).",
    )
    sample_size: int = Field(default=50, ge=1, le=10000)
    workers: list[int] = Field(
        default_factory=lambda: [1, 10, 20],
        description="Worker counts to benchmark (uncapped like the CLI repeatable --workers; each 1..64)",
    )

    @model_validator(mode="after")
    def _check_workers(self) -> IamBenchmarkRequest:
        for count in self.workers:
            if not 1 <= count <= 64:
                raise ValueError("workers entries must each be between 1 and 64")
        return self


class IamReportRequest(BaseModel):
    model_config = {"extra": "forbid"}

    job_id: str | None = Field(
        default=None,
        description="Audit/migrate job whose JSON report should be re-rendered",
    )
    json_path: str | None = Field(
        default=None,
        description=(
            "Job-scoped report filename (relative) or absolute path confined "
            "under the job base dir. Free absolute server paths are rejected. "
            "When both json_path and job_id are supplied, json_path wins and "
            "job_id is ignored (P2 #32)."
        ),
    )

    @model_validator(mode="after")
    def _require_one(self) -> IamReportRequest:
        if not self.json_path and not self.job_id:
            raise ValueError("Either json_path or job_id is required")
        return self


# -- validate ---------------------------------------------------------------
class ValidateRequest(ChainedRequest):
    live: bool = False
    resource_type: str | None = None
    skip_hosts: bool = False
    orgs: str | list[str] | None = Field(
        default=None, description="Comma-separated organization names to scope"
    )
    # Legacy spellings accepted at the boundary; canonicalized via
    # _shared.parse_organizations (single home). None = all.
    organizations: list[str] | None = Field(default=None)
    organization: str | None = Field(default=None)

    @model_validator(mode="after")
    def _check_hosts(self) -> ValidateRequest:
        if self.skip_hosts and self.resource_type == "hosts":
            raise ValueError("skip_hosts conflicts with resource_type=hosts")
        # Canonicalize via the single home (also rejects ambiguous and
        # unknown org spellings instead of silently widening to all).
        parse_organizations(self.model_dump())
        return self


class DependencyCheckRequest(ChainedRequest):
    resource_types: list[str] | None = Field(
        default=None,
        description="None (omitted) means all resource types; explicit [] is a no-op selecting none.",
    )


class PayloadCheckRequest(BaseModel):
    model_config = {"extra": "forbid"}

    resource_type: str
    payload: dict[str, Any]


# -- analysis -----------------------------------------------------------------
class AnalyzeDependenciesRequest(ChainedRequest):
    organizations: list[str] | None = Field(
        default=None,
        description="None (omitted) means all via analyze_all=true; "
        "explicit [] without analyze_all=true is 422 (not a no-op).",
    )
    analyze_all: bool = False
    verbose: bool = False
    # Legacy spellings accepted at the boundary; canonicalized via
    # _shared.parse_organizations (single home). None = all.
    organization: str | None = Field(default=None)
    orgs: str | list[str] | None = Field(default=None)

    @model_validator(mode="after")
    def _check_scope(self) -> AnalyzeDependenciesRequest:
        # Single home for spelling normalization + ambiguity/typo guards.
        scoped = parse_organizations(self.model_dump())
        # Explicit [] keeps its distinct meaning instead of collapsing to
        # the falsy fallback: only omission (None) falls back to the raw
        # field. Both are invalid without analyze_all (422 below), but the
        # shapes stay distinguishable for error reporting and callers.
        effective = scoped if scoped is not None else list(self.organizations or [])
        # Preserve original contract messages (omitted/None means all via
        # analyze_all; explicit [] is not a valid scope without analyze_all).
        if not self.analyze_all and not effective:
            raise ValueError("Must specify analyze_all=true or organizations=[...]")
        if self.analyze_all and effective:
            raise ValueError("Cannot use analyze_all with organizations")
        return self


# -- reporting ------------------------------------------------------------------
class MigrationReportRequest(ChainedRequest):
    resource_type: str | None = None
    by_organization: bool = False
    output_format: Literal["markdown", "csv", "html"] = "markdown"


class EnhancedReportRequest(ChainedRequest):
    resource_type: str | None = None
    output_format: Literal["html", "markdown", "csv"] = "html"
    organization: str | None = None
    # Legacy spellings accepted at the boundary; canonicalized via
    # _shared.parse_organizations (single home). None = all.
    organizations: list[str] | None = Field(default=None)
    orgs: str | list[str] | None = Field(default=None)

    @model_validator(mode="after")
    def _check_org_scope(self) -> EnhancedReportRequest:
        parse_organizations(self.model_dump())
        return self


class ProjectFailuresRequest(ChainedRequest):
    pass


# -- state / retry ---------------------------------------------------------------
class StateResetRequest(ConnectionSelector):
    resource_type: str | None = None
    keep_mappings: bool = False
    job_id: str | None = Field(
        default=None,
        description="When set, reset the chained job's state DB instead of "
        "the server-default state",
    )

    @model_validator(mode="after")
    def _reject_blank_resource_type(self) -> StateResetRequest:
        # Fail closed: blank (""/whitespace) is never a legitimate scope.
        # None/omitted already means "all", so a blank string that passes
        # the `not body.resource_type` truthiness test must 422 here
        # instead of taking the full-reset drop+reinit path.
        if self.resource_type is not None and not self.resource_type.strip():
            raise ValueError("resource_type must be a non-empty type or omitted for a full reset")
        return self


class StateExportRequest(ConnectionSelector):
    include_mappings: bool = True


class RetryFailedRequest(ChainedRequest):
    """Retry request: the chaining trio (job_id/allow_pair_switch/force).

    Extends :class:`ChainedRequest` instead of redeclaring the trio (P3
    #29): one definition, so a future chaining-rule change cannot drift
    the retry path from every other chained endpoint. Net-zero wire
    shape; ``force`` keeps the cancel-fence meaning from the base class.
    """

    resource_types: list[str] | None = Field(
        default=None,
        description="None (omitted) means all resource types; explicit [] is a no-op selecting none.",
    )
    dry_run: bool = False


class StateImportRequest(BaseModel):
    """Import a JSON state backup confined to the job file tree."""

    model_config = {"extra": "forbid"}

    state_file: str = Field(description="Job-scoped filename or path under the job base dir")
    job_id: str | None = Field(
        default=None, description="When set, confine state_file under that job's directory"
    )


class CheckpointCreate(BaseModel):
    phase: str = "manual"
    progress_stats: dict[str, Any] = Field(default_factory=dict)
    checkpoint_data: dict[str, Any] = Field(default_factory=dict)
    description: str = "API checkpoint"
    name: str | None = None
    job_id: str | None = Field(
        default=None,
        description="When set, create the checkpoint in the chained job's "
        "state DB instead of the server-default state",
    )

    model_config = {"extra": "forbid"}


# -- maintenance (prep / cleanup) -------------------------------------------------
class PrepRequest(ChainedRequest):
    force: bool = False


class CleanupRequest(ChainedRequest):
    resource_types: list[str] | None = Field(
        default=None,
        description="None (omitted) means all resource types; explicit [] is a no-op selecting none.",
    )
    full: bool = False
    db_only: bool = False
    rate_limit: int | None = Field(default=None, ge=1, le=1000)
    skip_dir: list[str] = Field(default_factory=list)
