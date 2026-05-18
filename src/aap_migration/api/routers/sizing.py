"""Sizing calculator endpoints for AAP 2.6 infrastructure recommendations."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aap_migration.api.crypto import decrypt_token
from aap_migration.api.dependencies import get_db
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.sizing.calculator import AAP26SizingCalculator

router = APIRouter()


class SizingRequest(BaseModel):
    managed_hosts: int = Field(..., ge=1)
    playbooks_per_day_peak: int = Field(..., ge=1)
    job_duration_hours: float = Field(default=0.5, ge=0.01)
    tasks_per_job: int = Field(default=50, ge=1)
    forks_observed: int = Field(default=10, ge=1)
    verbosity_level: int = Field(default=1, ge=0, le=4)
    allowed_hours_per_day: float = Field(default=8, ge=1, le=24)
    peak_pattern: str = Field(default="business_hours")
    deployment_target: Literal["ocp", "containerized"] = Field(
        default="ocp", description="Deployment target: ocp (OpenShift) or containerized (Podman)"
    )
    num_controllers: int = Field(default=2, ge=1, description="Number of controller nodes")
    concurrent_jobs: int = Field(default=0, ge=0, description="Max concurrent jobs (0=auto)")
    pending_jobs: int = Field(default=0, ge=0, description="Typical pending job count")
    job_retention_hours: int = Field(
        default=720, ge=24, description="Job history retention in hours"
    )
    fact_retention_hours: int = Field(
        default=720, ge=24, description="Fact cache retention in hours"
    )
    hub_nodes: int = Field(default=1, ge=0, description="Automation Hub nodes (0=disabled)")
    eda_nodes: int = Field(default=0, ge=0, description="EDA controller nodes (0=disabled)")


class SizingResponse(BaseModel):
    input: dict
    execution_nodes: dict
    controller: dict
    database: dict
    deployment: dict | None = None
    automation_hub: dict | None = None
    gateway: dict | None = None
    eda: dict | None = None
    redis: dict | None = None
    warnings: list[str] = []
    validation_warnings: list[str] = []


@router.post("/sizing/calculate", response_model=SizingResponse)
def calculate_sizing(data: SizingRequest) -> SizingResponse:
    calculator = AAP26SizingCalculator()
    metrics = data.model_dump()
    deployment_target = metrics.pop("deployment_target", "ocp")

    warnings: list[str] = []
    for key, val in metrics.items():
        if isinstance(val, int | float):
            w = calculator.validate_input(key, val)
            if w:
                warnings.extend(w)

    recommendation = calculator.generate_sizing_recommendation(metrics, deployment_target)

    validation_warnings: list[str] = []
    try:
        execution = recommendation["components"]["automation_controller_execution_plane"]
        controller = recommendation["components"]["automation_controller_control_plane"]
        database = recommendation["components"]["database"]
        vw = calculator.validate_results(execution, controller, database)
        if vw:
            validation_warnings.extend(vw)
    except (KeyError, TypeError):
        pass  # noqa: B110 — validation is best-effort, missing keys are non-fatal

    return SizingResponse(
        input=metrics,
        execution_nodes=recommendation["components"]["automation_controller_execution_plane"],
        controller=recommendation["components"]["automation_controller_control_plane"],
        database=recommendation["components"]["database"],
        deployment=recommendation.get("deployment"),
        automation_hub=recommendation["components"].get("automation_hub"),
        gateway=recommendation["components"].get("platform_gateway"),
        eda=recommendation["components"].get("event_driven_ansible"),
        redis=recommendation["components"].get("redis"),
        warnings=warnings + recommendation.get("warnings", []),
        validation_warnings=validation_warnings,
    )


class DynamicSizingRequest(BaseModel):
    connection_id: str = Field(..., description="ID of the source AAP connection to analyze")
    history_days: int = Field(
        default=30, ge=1, le=365, description="Days of job history to analyze"
    )
    deployment_target: Literal["ocp", "containerized"] = Field(
        default="ocp", description="Deployment target: ocp (OpenShift) or containerized (Podman)"
    )


class DynamicSizingResponse(BaseModel):
    mode: str
    deployment_target: str = "ocp"
    source_observed: dict
    derived_inputs: dict
    headroom_multiplier: float
    recommendation: dict


@router.post("/sizing/dynamic", response_model=DynamicSizingResponse)
def calculate_dynamic_sizing(
    data: DynamicSizingRequest, db: Session = Depends(get_db)
) -> DynamicSizingResponse:
    conn = ConnectionService.get(db, data.connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    token = decrypt_token(conn.token)
    if not token:
        raise HTTPException(
            status_code=400, detail="Connection has no authentication token configured"
        )

    explicit_prefix = getattr(conn, "api_prefix", None)
    default_prefix = "/api/v2" if conn.type == "awx" else "/api/controller/v2"
    base_url = conn.url.rstrip("/")

    # If the stored URL already ends with an API path, don't double it
    if explicit_prefix:
        api_prefix = explicit_prefix
    elif base_url.endswith(("/api/v2", "/api/controller/v2")):
        api_prefix = ""
    else:
        api_prefix = default_prefix

    auth_scheme = "Token" if getattr(conn, "type", "awx") == "awx" else "Bearer"

    from aap_migration.sizing.dynamic import calculate_dynamic_sizing as run_dynamic

    try:
        result = run_dynamic(
            base_url=base_url,
            token=token,
            api_prefix=api_prefix,
            verify_ssl=conn.verify_ssl,
            history_days=data.history_days,
            deployment_target=data.deployment_target,
            auth_scheme=auth_scheme,
        )
    except Exception as e:
        raise HTTPException(
            status_code=502, detail=f"Failed to collect metrics from AAP instance: {e}"
        ) from e

    return DynamicSizingResponse(**result)
