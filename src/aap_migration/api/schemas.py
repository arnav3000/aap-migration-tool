"""Pydantic request/response schemas for the API."""

from datetime import datetime

from pydantic import BaseModel


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
    name: str
    type: str
    status: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    result: dict | None = None


class MigrationPreviewRequest(BaseModel):
    source_id: str
    destination_id: str
    organizations: list[int] | None = None


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
