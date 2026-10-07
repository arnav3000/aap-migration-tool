"""Sync read envelopes and pinned v1 response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ._shared import JobStatus, JobStatusValue


# -- sync read envelopes (response models pin openapi success shapes) -----
# NOTE (P2 #12/#13 deprecation timeline): deprecated aliases below keep
# working but are removed in v2. Canonical shapes are errors_by_file (dict)
# for per-file maps and errors (list[str]) for validation result lists;
# artifacts keep walked (bounded) + total (true pre-page count). Old
# aliases carry deprecated=True so OpenAPI marks them; descriptions note
# "removed in v2". Do not reintroduce dict/list collisions under one name.
class StateShowOut(BaseModel):
    """GET /state/show success shape (models only constrain, no renames)."""

    model_config = {"extra": "allow"}

    migration_id: str | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    warning: str | None = None


class JobListOut(BaseModel):
    """GET /jobs envelope (true pre-page total)."""

    model_config = {"extra": "forbid"}

    items: list[JobStatus] = Field(default_factory=list)
    total: int = 0
    limit: int = 100
    offset: int = 0


class MappingOut(BaseModel):
    """One source->target ID mapping row (typed items for MappingsListOut)."""

    model_config = {"extra": "forbid"}

    resource_type: str
    source_id: int
    target_id: int | None = None
    source_name: str | None = None
    target_name: str | None = None


class CheckpointOut(BaseModel):
    """One migration checkpoint row (typed items for CheckpointsListOut)."""

    model_config = {"extra": "forbid"}

    id: int
    checkpoint_name: str | None = None
    phase: str | None = None
    progress_stats: dict[str, Any] | None = None
    created_at: Any = None
    description: str | None = None
    is_valid: bool | None = None


class MappingsListOut(BaseModel):
    """GET /state/mappings envelope.

    Uses the resource-named ``mappings`` key (not ``items``): the envelope
    is otherwise identical (``limit``/``offset``/``total``) and OpenAPI pins
    it here, so typed clients reuse one pager with a per-route key.
    ``warning`` carries the lenient 200+warning branch (missing DB) so the
    served field is part of the pinned OpenAPI shape.
    """

    model_config = {"extra": "allow"}

    mappings: list[MappingOut] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0
    warning: str | None = None


class CheckpointsListOut(BaseModel):
    """GET /checkpoints envelope (resource-named ``checkpoints`` key).

    ``warning`` carries the lenient 200+warning branch (missing DB) so the
    served field is part of the pinned OpenAPI shape.
    """

    model_config = {"extra": "allow"}

    checkpoints: list[CheckpointOut] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0
    warning: str | None = None


class ResourcesOut(BaseModel):
    """GET /resources catalog shape."""

    model_config = {"extra": "forbid"}

    all: list[str] = Field(default_factory=list)
    fully_supported: list[str] = Field(default_factory=list)
    migration_order: list[str] = Field(default_factory=list)
    cleanup_order: list[str] = Field(default_factory=list)
    resources: dict[str, Any] = Field(default_factory=dict)


class PrepSchemasOut(BaseModel):
    """GET /prep/schemas shape (canonical errors_by_file dict).

    Only ``errors_by_file`` (dict) is served here. Validation results
    elsewhere use ``errors`` as list[str] under their own response models;
    the names no longer collide on one route, so a shared ``errors`` parser
    cannot mis-decode. Removed in v2: none (the old ``errors`` dict alias
    was removed; read ``errors_by_file``).
    """

    model_config = {"extra": "allow"}

    errors_by_file: dict[str, str] = Field(default_factory=dict)


class JobArtifactsOut(BaseModel):
    """GET /jobs/{id}/artifacts shape (v1 frozen).

    total is the true pre-page count (full filtered walk); walked is the
    bounded-walk count so far (limit+offset+1 cap). truncated alone signals
    incompleteness. v1 freezes one meaning per field: total is always the
    true count, walked is always bounded. New clients read walked for
    bounded progress and total for the true count.
    """

    model_config = {"extra": "forbid"}

    job_id: str
    artifacts: list[str] = Field(default_factory=list)
    walked: int = 0
    total: int = Field(
        default=0,
        description="True pre-page count (full filtered walk).",
    )
    truncated: bool = False
    limit: int = 500
    offset: int = 0


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
    """GET /migrations/status shape (warning pins lenient 200+warning branch)."""

    model_config = {"extra": "allow"}

    migration_id: str | None = None
    resource_stats: dict[str, Any] = Field(default_factory=dict)
    unreadable_types: list[str] = Field(default_factory=list)
    warning: str | None = None


class DependencyCheckOut(BaseModel):
    model_config = {"extra": "allow"}

    requested: list[str] = Field(default_factory=list)
    dependency_closure: list[str] = Field(default_factory=list)
    missing: dict[str, Any] = Field(default_factory=dict)


class ValidationDependencyOut(BaseModel):
    """Wire shape for POST /validations/dependencies (not the import check)."""

    model_config = {"extra": "forbid"}

    input_dir: str
    validation: dict[str, Any] = Field(default_factory=dict)


class PayloadCheckOut(BaseModel):
    model_config = {"extra": "allow"}

    resource_type: str
    valid: bool
    errors: list[str] = Field(default_factory=list)


class ConfigValidateOut(BaseModel):
    """POST /config/validate success shape.

    ``valid`` is a boolean success marker (``True`` on 200; real failures
    use HTTP 400/502, never ``valid: false``): branch on the status code,
    not the boolean, for failure detection.
    """

    model_config = {"extra": "forbid"}

    valid: bool
    summary: dict[str, Any] = Field(default_factory=dict)
    connectivity: dict[str, Any] = Field(default_factory=dict)


class ConnectionTestOut(BaseModel):
    """POST /connections/{id}/test success shape (same boolean rule)."""

    model_config = {"extra": "forbid"}

    connection_id: str
    reachable: bool
    version: str | None = None
    url: str


# -- v1 pinned sync envelopes (P1 #1) --------------------------------------
# Every sync (non-202) route pins a response_model so v1 clients codegen
# against declared schemas instead of observed keys. Models use
# extra=forbid where the served shape is fixed; open shapes keep
# extra=allow with every served key declared.


class MigrationPlanOut(BaseModel):
    """GET /analysis/migration-plan shape."""

    model_config = {"extra": "forbid"}

    description: str
    helpers: list[str] = Field(default_factory=list)


class ConfigShowOut(BaseModel):
    """POST /config/show shape (tokens masked, fixed key set)."""

    model_config = {"extra": "forbid"}

    source_url: str
    target_url: str
    source_verify_ssl: bool
    target_verify_ssl: bool
    state_db_path: str
    default_batch_size: int | None = None
    host_batch_size: int | None = None
    max_concurrent: int | None = None
    rate_limit: int | None = None
    source_token: str = ""
    target_token: str = ""
    batch_sizes: dict[str, Any] = Field(default_factory=dict)


class DeleteConnectionOut(BaseModel):
    """DELETE /connections/{id} shape (distinct delete key per type, P2 #13)."""

    model_config = {"extra": "forbid"}

    deleted_connection_id: str


class DeleteJobOut(BaseModel):
    """DELETE /jobs/{id} shape (distinct delete key per type, P2 #13)."""

    model_config = {"extra": "forbid"}

    deleted_job_id: str


class DeleteCheckpointOut(BaseModel):
    """DELETE /checkpoints/{id} shape (distinct delete key per type, P2 #13)."""

    model_config = {"extra": "forbid"}

    deleted_checkpoint_id: int


class IamCheckpointOut(BaseModel):
    """GET /iam/checkpoint shape."""

    model_config = {"extra": "forbid"}

    description: str
    checkpoint_file: str


class JobConsoleOut(BaseModel):
    """GET /jobs/{id}/console shape."""

    model_config = {"extra": "forbid"}

    job_id: str
    console: str = ""
    console_available: bool = False


class JobCancelOut(BaseModel):
    """POST /jobs/{id}/cancel shape (narrowed status plus pending flag)."""

    model_config = {"extra": "forbid"}

    job_id: str
    status: JobStatusValue
    cancel_pending: bool = False


class TransformPreviewOut(BaseModel):
    """POST /transforms/preview shape (validation *result*, not an error)."""

    model_config = {"extra": "forbid"}

    resource_type: str
    valid: bool
    errors: list[str] = Field(default_factory=list)
    transformed: Any = None
    preview: bool = True


class ImportDepCheckOut(BaseModel):
    """POST /imports/check-dependencies shape (missing is a list here,
    unlike the validations route whose missing is a dict)."""

    model_config = {"extra": "forbid"}

    requested: list[str] = Field(default_factory=list)
    dependency_closure: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class StateResetOut(BaseModel):
    """POST /state/reset shape (one envelope on every branch)."""

    model_config = {"extra": "forbid"}

    resource_type: str | None = None
    cleared_progress: int = 0
    reset_mappings: int = 0
    reset: str | None = None
    keep_mappings: bool = False


class StateImportOut(BaseModel):
    """POST /state/import shape."""

    model_config = {"extra": "forbid"}

    imported: str
    scope: Literal["job", "server-default"]


class RetryStatusOut(BaseModel):
    """GET /retry/status shape (warning pins the lenient 200 branch)."""

    model_config = {"extra": "forbid"}

    by_type: dict[str, Any] = Field(default_factory=dict)
    warning: str | None = None


class ResumeInfoOut(BaseModel):
    """GET /checkpoints/resume-info shape (stable key set, P2 #21)."""

    model_config = {"extra": "forbid"}

    resumable: bool = False
    resume_from: dict[str, Any] | None = None
    warning: str | None = None


class CheckpointCreateOut(BaseModel):
    """POST /checkpoints shape."""

    model_config = {"extra": "forbid"}

    checkpoint_id: int
    migration_id: str | None = None


class ResourceDetailOut(BaseModel):
    """GET /resources/{type} shape."""

    model_config = {"extra": "forbid"}

    name: str
    endpoint: str
    description: str
    migration_order: int
    cleanup_order: int
    has_exporter: bool = True
    has_importer: bool = False
    has_transformer: bool = False
    batch_size: int = 100
