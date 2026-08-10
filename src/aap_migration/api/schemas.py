"""Pydantic request/response schemas for the API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ConnectionCreate(BaseModel):
    name: str
    url: str
    token: str | None = None
    type: str = "awx"
    role: str = "source"
    verify_ssl: bool = True
    timeout: int = 30


class ConnectionUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    token: str | None = None
    type: str | None = None
    role: str | None = None
    verify_ssl: bool | None = None
    timeout: int | None = None


class ConnectionResponse(BaseModel):
    id: str
    name: str
    url: str
    token: str = Field(exclude=True)
    role: str
    verify_ssl: bool
    timeout: int
    ping_status: str
    auth_status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConnectionResponseMasked(BaseModel):
    """Connection response with masked token."""

    id: str
    name: str
    url: str
    type: str
    role: str
    verify_ssl: bool
    timeout: int
    ping_status: str
    auth_status: str
    has_token: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TestConnectionResponse(BaseModel):
    ok: bool
    error: str | None = None


class JobStartResponse(BaseModel):
    job_id: str


class JobResponse(BaseModel):
    id: str
    seq_id: int | None = None
    name: str
    type: str
    status: str
    created_at: str
    started_at: str
    finished_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    output: list[str] | None = None


class JobSummaryResponse(BaseModel):
    id: str
    seq_id: int | None = None
    name: str
    type: str
    status: str
    created_at: str
    started_at: str
    finished_at: str | None = None
    error: str | None = None


class MigrationPreviewRequest(BaseModel):
    source_id: str
    destination_id: str
    organizations: list[int] | None = None
    name_prefix: str | None = None


class MigrationRunRequest(BaseModel):
    source_id: str
    destination_id: str
    job_id: str
    exclusions: dict[str, list[int]] | None = None
    organizations: list[int] | None = None
    name_prefix: str | None = None


class AnalysisRunRequest(BaseModel):
    connection_id: str


class SizingRequest(BaseModel):
    model_config = {"extra": "allow"}


class DynamicSizingRequest(BaseModel):
    connection_id: str
    history_days: int = 30
    deployment_target: str = "ocp"


class ClearStateResponse(BaseModel):
    cleared_progress: int
    deleted_mappings: int


class ConcurrencySettingResponse(BaseModel):
    max_concurrent: int = Field(default=15, ge=1, le=100)


class ConcurrencySettingUpdate(BaseModel):
    max_concurrent: int = Field(ge=1, le=100)


class SelectiveMigrateRequest(BaseModel):
    source_id: str
    destination_id: str
    job_template_ids: list[int] = Field(default_factory=list)
    workflow_job_template_ids: list[int] = Field(default_factory=list)
    force_update: bool = False
    name_prefix: str | None = None

    @model_validator(mode="after")
    def require_at_least_one_template(self) -> "SelectiveMigrateRequest":
        if not self.job_template_ids and not self.workflow_job_template_ids:
            raise ValueError("At least one job_template_id or workflow_job_template_id is required")
        return self


# --- Migration Planner Schemas ---


class PlanSourceCreate(BaseModel):
    connection_id: str
    name_prefix: str | None = None
    analysis_job_id: str | None = None


class PlanCreate(BaseModel):
    name: str
    description: str = ""
    destination_id: str
    sources: list[PlanSourceCreate] = []


class PlanUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    destination_id: str | None = None
    status: str | None = None


class PlanSourceUpdate(BaseModel):
    id: str | None = None
    connection_id: str
    name_prefix: str | None = None
    analysis_job_id: str | None = None


class PhaseOrgUpdate(BaseModel):
    source_id: str
    org_id: int
    org_name: str


class PhaseUpdate(BaseModel):
    id: str | None = None
    phase_number: int
    name: str = ""
    update_mode: bool = False
    resource_types: list[str] | None = None
    orgs: list[PhaseOrgUpdate] = []


class PhasesUpdateRequest(BaseModel):
    phases: list[PhaseUpdate]
    sources: list[PlanSourceUpdate] | None = None


class PlanPhaseOrgResponse(BaseModel):
    id: str
    source_id: str
    org_id: int
    org_name: str

    model_config = {"from_attributes": True}


class PlanPhaseResponse(BaseModel):
    id: str
    phase_number: int
    name: str
    status: str
    update_mode: bool = False
    resource_types: list[str] = []
    job_id: str | None = None
    orgs: list[PlanPhaseOrgResponse] = []

    model_config = {"from_attributes": True}


class PlanSourceResponse(BaseModel):
    id: str
    connection_id: str
    name_prefix: str | None = None
    analysis_job_id: str | None = None

    model_config = {"from_attributes": True}


class PlanResponse(BaseModel):
    id: str
    name: str
    description: str
    status: str
    destination_id: str | None
    created_at: datetime
    updated_at: datetime
    sources: list[PlanSourceResponse] = []
    phases: list[PlanPhaseResponse] = []

    model_config = {"from_attributes": True}


class PlanListItem(BaseModel):
    id: str
    name: str
    description: str
    status: str
    destination_id: str | None
    created_at: datetime
    updated_at: datetime
    source_count: int = 0
    phase_count: int = 0

    model_config = {"from_attributes": True}


# --- IAM API Schemas ---


class IAMAnalyseRequest(BaseModel):
    connection_id: str
    output_dir: str = "./iam_reports/"
    verify_ssl: bool | None = None
    timeout: int = Field(default=60, ge=1, le=600)
    workers: int = Field(default=1, ge=1, le=100)
    scan_strategy: str = Field(default="resource", pattern="^(resource|principal)$")
    resume: bool = False
    checkpoint_dir: str | None = None


class IAMBenchmarkRequest(BaseModel):
    connection_id: str
    verify_ssl: bool | None = None
    sample_size: int = Field(default=50, ge=1, le=500)
    workers: list[int] | None = None


class IAMReportRequest(BaseModel):
    json_path: str | None = None
    output_dir: str | None = None
    job_id: str | None = None

    @model_validator(mode="after")
    def require_json_source(self) -> "IAMReportRequest":
        if not self.json_path and not self.job_id:
            raise ValueError("Either json_path or job_id is required")
        return self


# --- Discrete ETL API Schemas ---


class MigrationExportRequest(BaseModel):
    source_id: str
    resource_types: list[str] | None = None
    output_dir: str | None = None
    organizations: list[int] | None = None
    records_per_file: int = Field(default=1000, ge=1, le=10000)
    resume: bool = False


class MigrationTransformRequest(BaseModel):
    input_dir: str | None = None
    export_job_id: str | None = None
    output_dir: str | None = None
    resource_types: list[str] | None = None
    destination_id: str | None = None
    defer_project_sync: bool = True

    @model_validator(mode="after")
    def require_input_source(self) -> "MigrationTransformRequest":
        if not self.input_dir and not self.export_job_id:
            raise ValueError("Either input_dir or export_job_id is required")
        return self


class MigrationImportRequest(BaseModel):
    source_id: str
    destination_id: str
    input_dir: str | None = None
    transform_job_id: str | None = None
    resource_types: list[str] | None = None
    name_prefix: str | None = None
    organizations: list[int] | None = None
    dry_run: bool = False

    @model_validator(mode="after")
    def require_input_source(self) -> "MigrationImportRequest":
        if not self.input_dir and not self.transform_job_id:
            raise ValueError("Either input_dir or transform_job_id is required")
        return self
