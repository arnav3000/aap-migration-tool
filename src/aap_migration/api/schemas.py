"""Pydantic request/response schemas for the API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, Field

# --- Health / Version ---


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    api_prefix: str = "/api"


class VersionResponse(BaseModel):
    version: str
    title: str = "AAP Bridge API"


# --- Connections ---


class ConnectionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    url: str = Field(..., min_length=1, max_length=512)
    token: str = Field(..., min_length=1)
    role: str = Field(default="source", pattern="^(source|target)$")
    verify_ssl: bool = True
    timeout: int = Field(default=30, ge=1, le=1200)


class ConnectionUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    token: str | None = None
    role: str | None = Field(default=None, pattern="^(source|target)$")
    verify_ssl: bool | None = None
    timeout: int | None = Field(default=None, ge=1, le=1200)


class ConnectionResponse(BaseModel):
    id: str
    name: str
    url: str
    role: str
    verify_ssl: bool
    timeout: int
    ping_status: str | None = None
    auth_status: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class ConnectionResponseMasked(ConnectionResponse):
    """Connection response with masked token."""

    token_masked: str = "***"


class TestConnectionResponse(BaseModel):
    ping_status: str
    auth_status: str
    error: str | None = None


# --- Jobs ---


class JobStartResponse(BaseModel):
    job_id: str
    seq_id: int | None = None


class JobResponse(BaseModel):
    id: str
    seq_id: int | None = None
    type: str
    status: str
    name: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    output: dict[str, Any] | None = None


# --- Resources ---


class ResourceTypeInfoResponse(BaseModel):
    name: str
    endpoint: str
    description: str
    migration_order: int
    cleanup_order: int
    has_exporter: bool
    has_importer: bool
    has_transformer: bool
    batch_size: int
    use_bulk_api: bool


class ResourcesListResponse(BaseModel):
    count: int
    resources: list[ResourceTypeInfoResponse]


# --- Migration ---


class MigrationPreviewRequest(BaseModel):
    source_id: str = Field(..., description="Source connection ID")
    destination_id: str = Field(
        ...,
        description="Target connection ID (also accepts target_id)",
        validation_alias=AliasChoices("destination_id", "target_id"),
    )
    resource_types: list[str] | None = Field(
        default=None, description="Subset of resource types to preview"
    )
    organizations: list[str] | None = Field(
        default=None, description="Filter to specific organization names"
    )
    name_prefix: str = Field(
        default="", description="Only count resources whose name starts with prefix"
    )

    model_config = {"populate_by_name": True}


class MigrationRunRequest(BaseModel):
    source_id: str = Field(..., description="Source connection ID")
    destination_id: str = Field(
        ...,
        description="Target connection ID (also accepts target_id)",
        validation_alias=AliasChoices("destination_id", "target_id"),
    )
    resource_types: list[str] | None = Field(default=None, description="Subset to migrate")
    organizations: list[str] | None = Field(default=None, description="Filter to org names")
    name_prefix: str = Field(default="", description="Name prefix filter")
    dry_run: bool = Field(default=False, description="If true, export+transform only, no import")
    skip_validation: bool = Field(default=False, description="Skip validation steps")

    model_config = {"populate_by_name": True}


class MigrationPreviewResponse(BaseModel):
    job_id: str
    status: str
    counts: dict[str, int] = Field(default_factory=dict)
    resource_types: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    result: dict[str, Any] | None = None


class ClearStateRequest(BaseModel):
    resource_types: list[str] | None = None


class ClearStateResponse(BaseModel):
    cleared: int
    message: str = "Migration state cleared"


# --- Task 4: Operations / Planner / Analysis (clean rewrite) ---


class AnalysisRunRequest(BaseModel):
    connection_id: str = Field(..., description="Source connection ID for analysis")
    organizations: list[str] | None = Field(
        default=None, description="Specific org names; if omitted, analyze all orgs"
    )


class AnalysisResponse(BaseModel):
    job_id: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None


class ExportRequest(BaseModel):
    connection_id: str = Field(..., description="Connection ID to export from")
    resource_types: list[str] | None = Field(default=None, description="Subset to export")


class CleanupRequest(BaseModel):
    connection_id: str = Field(..., description="Target connection ID to clean up")
    resource_types: list[str] = Field(..., description="Resource types to delete from target")


class PlanCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    source_id: str | None = Field(default=None, description="Source connection ID")
    target_id: str | None = Field(
        default=None,
        description="Target connection ID (alias destination_id)",
        validation_alias=AliasChoices("target_id", "destination_id"),
    )
    phases: list[dict[str, Any]] | None = Field(
        default=None, description="Optional phases definition"
    )

    model_config = {"populate_by_name": True}


class PlanUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    source_id: str | None = None
    target_id: str | None = Field(
        default=None, validation_alias=AliasChoices("target_id", "destination_id")
    )
    status: str | None = None
    phases: list[dict[str, Any]] | None = None

    model_config = {"populate_by_name": True}


class PlanResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    source_id: str | None = None
    target_id: str | None = None
    status: str
    phases: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class PlansListResponse(BaseModel):
    count: int
    plans: list[PlanResponse]


class ResourceTypeDetailResponse(BaseModel):
    name: str
    description: str
    migration_order: int
    cleanup_order: int
    has_exporter: bool
    has_importer: bool
    has_transformer: bool
    dependencies: dict[str, str] = Field(default_factory=dict)


# --- Task 5: IAM / Validate / Sizing / Settings (clean) ---


class IAMAuditRequest(BaseModel):
    source_id: str = Field(..., description="Source connection ID")
    scan_strategy: str = Field(default="resource", pattern="^(resource|principal)$")
    workers: int = Field(default=1, ge=1, le=16)


class IAMMigrateRequest(BaseModel):
    source_id: str = Field(..., description="Source connection ID")
    destination_id: str = Field(
        ...,
        validation_alias=AliasChoices("destination_id", "target_id"),
        description="Target connection ID",
    )
    scan_strategy: str = Field(default="resource", pattern="^(resource|principal)$")
    workers: int = Field(default=1, ge=1, le=16)
    dry_run: bool = Field(default=False)
    skip_user_roles: bool = Field(default=False)

    model_config = {"populate_by_name": True}


class ValidateRunRequest(BaseModel):
    source_id: str | None = Field(default=None, description="Source connection ID")
    destination_id: str | None = Field(
        default=None, validation_alias=AliasChoices("destination_id", "target_id")
    )
    live: bool = Field(default=False, description="Compare live target via API instead of DB")
    resource_type: str | None = Field(default=None)
    skip_hosts: bool = Field(default=False)
    organizations: list[str] | None = None

    model_config = {"populate_by_name": True}


class SizingCalculateRequest(BaseModel):
    connection_id: str = Field(..., description="Connection ID to size")
    resource_types: list[str] | None = Field(default=None)


class SizingDynamicRequest(BaseModel):
    connection_id: str = Field(..., description="Connection ID")
    resource_types: list[str] | None = None
    sample_size: int | None = Field(default=None, ge=1)


class SizingResponse(BaseModel):
    connection_id: str
    resource_types: list[str]
    total_resources: int
    counts: dict[str, int]
    recommended_batch_size: int
    estimated_duration_seconds: int


class SettingsConcurrencyResponse(BaseModel):
    max_concurrent: int = Field(default=5, ge=1, le=50)
    batch_size: int = Field(default=200, ge=1, le=500)
    rate_limit: int = Field(default=20, ge=1, le=100)


class SettingsConcurrencyUpdate(BaseModel):
    max_concurrent: int | None = Field(default=None, ge=1, le=50)
    batch_size: int | None = Field(default=None, ge=1, le=500)
    rate_limit: int | None = Field(default=None, ge=1, le=100)
