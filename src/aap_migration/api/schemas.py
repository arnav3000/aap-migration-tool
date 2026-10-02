"""Pydantic request/response schemas for the REST API.

Field names and defaults mirror the ``release/v1.x`` click options 1:1 so the
API is a faithful superset of the CLI surface.

Contract notes (error envelope vs result envelope):

- Error responses always use ``{"detail": "<human message>"}`` with a plain
  string ``detail`` (FastAPI ``HTTPException`` plus the app-level
  ``ValueError``/``KeyError`` handlers). The single ``422`` case that used to
  return a dict detail now returns a string; structured data travels in the
  success result or a documented ``result`` payload.
- Validation *results* (payload checks, dependency gates) use ``200`` with an
  explicit ``{"valid": bool, "errors": [...]}`` shape. That is a result, not
  an error envelope, and is documented per endpoint.
- Success *job results* share one envelope: every ``result`` dict carries
  ``message`` plus ``artifacts`` (possibly empty; workdir-relative paths).
  Per-type keys (``report``, ``summary``, ``stats``, ``migration_order``,
  ``comparison``, ...) ride alongside, never instead of, that pair.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

JobStatusValue = Literal["queued", "running", "succeeded", "failed", "cancelled"]
JobListFilter = JobStatusValue


# -- shared -------------------------------------------------------------
class ConnectionSelector(BaseModel):
    """Optionally override the active stored connections for one call."""

    model_config = {"extra": "forbid"}

    source_id: str | None = Field(
        default=None, description="Stored source connection id (default: active)"
    )
    target_id: str | None = Field(
        default=None, description="Stored target connection id (default: active)"
    )


class JobCreated(BaseModel):
    """Accepted-job response. Server-local paths are intentionally omitted."""

    job_id: str
    job_type: str
    status: JobStatusValue = "queued"
    poll_url: str = Field(
        default="",
        description="Host-relative poll path (e.g. /api/v1/jobs/<id>); "
        "resolve it against the API base URL in use.",
    )
    chained_from_status: JobStatusValue | None = Field(
        default=None,
        description="Status of the referenced job at submit time when chaining "
        "(echoes succeeded, failed, or cancelled on resume/retry paths; "
        "None when unchained)",
    )


class ChainedRequest(ConnectionSelector):
    """Requests that can continue work in an existing job directory.

    ETL phases share a working directory (exports/, xformed/, state DB).
    Pass the ``job_id`` of a previous job to chain phases, e.g. export, then
    ``POST /transforms {"job_id": "<export-job>"}``, then import, validate,
    and reporting against the same directory. Omit for a fresh isolated dir.

    Chaining requires the referenced job to have ``succeeded`` unless
    the endpoint chains onto failed/cancelled jobs (resume/retry) or
    ``allow_pair_switch``/partial-input semantics are explicitly opted into
    (see router errors). Switching AAP pairs mid-pipeline requires
    ``allow_pair_switch=true``.

    Unknown fields are rejected (``extra="forbid"`` inherited) so typos like
    ``resource_typs`` fail with 422 instead of running unscoped with 202.
    """

    job_id: str | None = Field(
        default=None,
        description="Existing job id whose working directory should be reused",
    )
    allow_pair_switch: bool = Field(
        default=False,
        description="Explicit opt-in to run a chained phase against a different AAP pair",
    )


class JobStatus(BaseModel):
    """Pollable job state. No server-local filesystem paths are exposed."""

    job_id: str
    job_type: str
    status: JobStatusValue
    params: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    error: str | None = None
    exit_code: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


# -- connections ---------------------------------------------------------
class ConnectionCreate(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    kind: Literal["source", "target"]
    url: str
    token: str
    verify_ssl: bool = True
    timeout: int = Field(default=30, ge=1, le=1200)


class ConnectionUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = None
    url: str | None = None
    token: str | None = None
    verify_ssl: bool | None = None
    timeout: int | None = Field(default=None, ge=1, le=1200)


class ConnectionReplace(BaseModel):
    """Full-replace body for PUT (all fields required; omitted fields reset)."""

    model_config = {"extra": "forbid"}

    name: str
    url: str
    token: str
    verify_ssl: bool = True
    timeout: int = Field(default=30, ge=1, le=1200)


class ConnectionOut(BaseModel):
    """Public connection view. The secret token is never serialized."""

    id: str
    name: str
    kind: str
    url: str
    verify_ssl: bool
    timeout: int
    created_at: str | None = None
    updated_at: str | None = None


class ActiveConfigIn(BaseModel):
    model_config = {"extra": "forbid"}

    source_id: str | None = None
    target_id: str | None = None
    clear_source: bool = Field(
        default=False,
        description="Set true to explicitly detach the active source (None keeps the current value)",
    )
    clear_target: bool = Field(
        default=False,
        description="Set true to explicitly detach the active target (None keeps the current value)",
    )


class ActiveConfigOut(BaseModel):
    source_id: str | None = None
    target_id: str | None = None


# -- config --------------------------------------------------------------
class ConfigValidateRequest(ConnectionSelector):
    check_connectivity: bool = False


class ConfigShowRequest(ConnectionSelector):
    pass


# -- migrate / ETL --------------------------------------------------------
class MigrateRequest(ChainedRequest):
    resource_types: list[str] | None = Field(
        default=None,
        description="None (omitted) means all resource types; explicit [] is a no-op selecting none.",
    )
    force: bool = False
    resume: bool = False
    skip_prep: bool = False
    phase: Literal["phase1", "phase2", "all"] = "all"


class MigrateResumeRequest(ChainedRequest):
    from_phase: str | None = None
    disable_progress: bool = False
    quiet: bool = False


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
    phase: Literal["phase1", "phase2", "phase3", "all"] = "all"


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
        default=None, description="Override stored connection verify_ssl (default: stored value)"
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
    verify_ssl: bool | None = Field(default=None, description="Override stored verify_ssl")
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
            "under the job base dir. Free absolute server paths are rejected."
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
    orgs: str | None = Field(
        default=None, description="Comma-separated organization names to scope"
    )

    @model_validator(mode="after")
    def _check_hosts(self) -> ValidateRequest:
        if self.skip_hosts and self.resource_type == "hosts":
            raise ValueError("skip_hosts conflicts with resource_type=hosts")
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
    organizations: list[str] = Field(default_factory=list)
    analyze_all: bool = False
    verbose: bool = False

    @model_validator(mode="after")
    def _check_scope(self) -> AnalyzeDependenciesRequest:
        if not self.analyze_all and not self.organizations:
            raise ValueError("Must specify analyze_all=true or organizations=[...]")
        if self.analyze_all and self.organizations:
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


class StateExportRequest(ConnectionSelector):
    include_mappings: bool = True


class RetryFailedRequest(ConnectionSelector):
    resource_types: list[str] | None = Field(
        default=None,
        description="None (omitted) means all resource types; explicit [] is a no-op selecting none.",
    )
    dry_run: bool = False
    job_id: str | None = Field(
        default=None,
        description="When set, retry against the chained job's workdir and "
        "state DB instead of the server-default state",
    )
    allow_pair_switch: bool = Field(
        default=False,
        description="Explicit opt-in to retry under a different AAP pair "
        "than the referenced job was submitted with",
    )


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


# -- sync read envelopes (response models pin openapi success shapes) -----
class HealthOut(BaseModel):
    model_config = {"extra": "allow"}

    status: str
    service: str
    version: str
    worker: str
    queue_depth: int | None = None


class ReadyOut(BaseModel):
    model_config = {"extra": "allow"}

    ready: bool
    checks: dict[str, Any] = Field(default_factory=dict)
    queue_depth: int | None = None


class VersionOut(BaseModel):
    model_config = {"extra": "allow"}

    api: str
    prog_name: str


class MigrationStatusOut(BaseModel):
    model_config = {"extra": "allow"}

    migration_id: str | None = None
    resource_stats: dict[str, Any] = Field(default_factory=dict)
    unreadable_types: list[str] = Field(default_factory=list)


class DependencyCheckOut(BaseModel):
    model_config = {"extra": "allow"}

    requested: list[str] = Field(default_factory=list)
    dependency_closure: list[str] = Field(default_factory=list)
    missing: dict[str, Any] = Field(default_factory=dict)


class PayloadCheckOut(BaseModel):
    model_config = {"extra": "allow"}

    resource_type: str
    valid: bool
    errors: list[str] = Field(default_factory=list)
