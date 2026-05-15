from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ConnectionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    type: Literal["awx", "aap"]
    role: Literal["source", "destination"]
    url: str = Field(..., min_length=1, max_length=512)
    token: str | None = None
    verify_ssl: bool = True


class ConnectionUpdate(BaseModel):
    name: str | None = None
    type: Literal["awx", "aap"] | None = None
    role: Literal["source", "destination"] | None = None
    url: str | None = None
    token: str | None = None
    verify_ssl: bool | None = None


class ConnectionResponse(BaseModel):
    id: str
    name: str
    type: str
    role: str
    url: str
    token: str | None = ""
    verify_ssl: bool
    version: str | None = None
    api_prefix: str | None = None
    ping_status: str = "unknown"
    ping_error: str | None = None
    auth_status: str = "unknown"
    auth_error: str | None = None
    last_checked: datetime | None = None

    model_config = {"from_attributes": True}


class TestResult(BaseModel):
    ok: bool
    ping_status: str
    auth_status: str
    version: str | None = None
    api_prefix: str | None = None
    error: str | None = None


class JobResponse(BaseModel):
    id: str
    type: str
    connection_id: str | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None

    model_config = {"from_attributes": True}


class JobDetailResponse(JobResponse):
    output: list[str] = []
    job_metadata: dict | None = None


class MigratePreviewRequest(BaseModel):
    source_id: str
    destination_id: str


class MigrateRunRequest(BaseModel):
    source_id: str
    destination_id: str
    job_id: str
    exclusions: dict[str, list[int]] | None = None


class MigrationResource(BaseModel):
    source_id: int
    name: str
    type: str
    action: str
    dest_id: int | None = None


class MigrationPreviewResponse(BaseModel):
    source_id: str
    destination_id: str
    resources: dict[str, list[MigrationResource]] = {}
    warnings: list[str] = []
    host_counts: dict[str, int] = {}
    group_counts: dict[str, int] = {}


class JobCreatedResponse(BaseModel):
    job_id: str
