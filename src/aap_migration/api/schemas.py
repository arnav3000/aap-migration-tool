"""Pydantic request/response schemas for the API."""

from datetime import datetime

from pydantic import BaseModel, Field


class ConnectionCreate(BaseModel):
    name: str
    url: str
    token: str
    role: str = "source"
    verify_ssl: bool = True
    timeout: int = 30


class ConnectionUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    token: str | None = None
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


class MigrationRunRequest(BaseModel):
    source_id: str
    destination_id: str
    job_id: str
    exclusions: dict[str, list[int]] | None = None


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
