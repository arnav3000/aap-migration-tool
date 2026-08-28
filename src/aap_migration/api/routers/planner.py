"""Planner API (Task 4 clean) — resource-types + plans CRUD."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, verify_api_token
from aap_migration.api.models import MigrationPlan
from aap_migration.api.schemas import (
    PlanCreateRequest,
    PlanResponse,
    PlansListResponse,
    PlanUpdateRequest,
    ResourceTypeDetailResponse,
)
from aap_migration.resources import RESOURCE_REGISTRY

router = APIRouter(prefix="/planner", tags=["planner"], dependencies=[Depends(verify_api_token)])


def _get_importer_deps(resource_type: str) -> dict[str, str]:
    try:
        from aap_migration.migration.importer import IMPORTER_REGISTRY  # type: ignore

        cls = IMPORTER_REGISTRY.get(resource_type)
        if cls and hasattr(cls, "DEPENDENCIES"):
            return dict(cls.DEPENDENCIES)
    except Exception:
        pass
    return {}


def _plan_to_response(plan: MigrationPlan) -> PlanResponse:
    phases: list[dict[str, Any]] = []
    if plan.phases_json:
        try:
            phases = json.loads(plan.phases_json)
        except Exception:
            phases = []
    return PlanResponse(
        id=plan.id,
        name=plan.name,
        description=plan.description,
        source_id=plan.source_id,
        target_id=plan.target_id,
        status=plan.status,
        phases=phases,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


@router.get("/resource-types", response_model=list[ResourceTypeDetailResponse])
async def list_resource_types() -> list[ResourceTypeDetailResponse]:
    out: list[ResourceTypeDetailResponse] = []
    for rtype, info in RESOURCE_REGISTRY.items():
        out.append(
            ResourceTypeDetailResponse(
                name=rtype,
                description=info.description,
                migration_order=info.migration_order,
                cleanup_order=info.cleanup_order,
                has_exporter=info.has_exporter,
                has_importer=info.has_importer,
                has_transformer=info.has_transformer,
                dependencies=_get_importer_deps(rtype),
            )
        )
    out.sort(key=lambda x: x.migration_order)
    return out


@router.post("/plans", response_model=PlanResponse, status_code=201)
async def create_plan(body: PlanCreateRequest, db: Session = Depends(get_db)) -> PlanResponse:
    # Validate source/target if provided
    from aap_migration.api.services.connection_service import ConnectionService

    if body.source_id and not ConnectionService.get(db, body.source_id):
        raise HTTPException(status_code=404, detail="Source connection not found")
    if body.target_id and not ConnectionService.get(db, body.target_id):
        raise HTTPException(status_code=404, detail="Target connection not found")
    phases_json = json.dumps(body.phases) if body.phases is not None else None
    plan = MigrationPlan(
        id=str(uuid.uuid4()),
        name=body.name,
        description=body.description,
        source_id=body.source_id,
        target_id=body.target_id,
        status="draft",
        phases_json=phases_json,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return _plan_to_response(plan)


@router.get("/plans", response_model=PlansListResponse)
async def list_plans(db: Session = Depends(get_db)) -> PlansListResponse:
    plans = db.query(MigrationPlan).order_by(MigrationPlan.created_at.desc()).all()
    return PlansListResponse(count=len(plans), plans=[_plan_to_response(p) for p in plans])


@router.get("/plans/{plan_id}", response_model=PlanResponse)
async def get_plan(plan_id: str, db: Session = Depends(get_db)) -> PlanResponse:
    plan = db.get(MigrationPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return _plan_to_response(plan)


@router.put("/plans/{plan_id}", response_model=PlanResponse)
async def update_plan(
    plan_id: str, body: PlanUpdateRequest, db: Session = Depends(get_db)
) -> PlanResponse:
    plan = db.get(MigrationPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if body.name is not None:
        plan.name = body.name
    if body.description is not None:
        plan.description = body.description
    if body.source_id is not None:
        from aap_migration.api.services.connection_service import ConnectionService

        if body.source_id and not ConnectionService.get(db, body.source_id):
            raise HTTPException(status_code=404, detail="Source connection not found")
        plan.source_id = body.source_id
    if body.target_id is not None:
        from aap_migration.api.services.connection_service import ConnectionService

        if body.target_id and not ConnectionService.get(db, body.target_id):
            raise HTTPException(status_code=404, detail="Target connection not found")
        plan.target_id = body.target_id
    if body.status is not None:
        plan.status = body.status
    if body.phases is not None:
        plan.phases_json = json.dumps(body.phases)
    db.commit()
    db.refresh(plan)
    return _plan_to_response(plan)


@router.delete("/plans/{plan_id}", status_code=204)
async def delete_plan(plan_id: str, db: Session = Depends(get_db)) -> None:
    plan = db.get(MigrationPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    db.delete(plan)
    db.commit()
    return None


@router.post("/plans/{plan_id}/populate", response_model=PlanResponse)
async def populate_plan(plan_id: str, db: Session = Depends(get_db)) -> PlanResponse:
    """Populate plan phases from source analysis (stub: returns current plan).
    In full implementation this would trigger analysis and build phases.
    """
    plan = db.get(MigrationPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    # For clean rewrite, we just mark as populated if empty
    if not plan.phases_json:
        types = sorted(RESOURCE_REGISTRY.keys(), key=lambda t: RESOURCE_REGISTRY[t].migration_order)
        # Group into 3 phases for demo: foundation, infra, automation
        phases = [
            {"name": "foundation", "resource_types": types[:5]},
            {"name": "infrastructure", "resource_types": types[5:12]},
            {"name": "automation", "resource_types": types[12:]},
        ]
        plan.phases_json = json.dumps(phases)
        plan.status = "populated"
        db.commit()
        db.refresh(plan)
    return _plan_to_response(plan)


@router.post("/plans/{plan_id}/execute", response_model=dict)
async def execute_plan(plan_id: str, db: Session = Depends(get_db)) -> dict:
    """Execute plan — starts a background migration job for the plan's phases."""
    plan = db.get(MigrationPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if not plan.source_id or not plan.target_id:
        raise HTTPException(status_code=422, detail="Plan missing source_id/target_id")
    from aap_migration.api.dependencies import get_job_service
    from aap_migration.api.services.connection_service import ConnectionService

    source = ConnectionService.get(db, plan.source_id)
    target = ConnectionService.get(db, plan.target_id)
    if not source or not target:
        raise HTTPException(status_code=404, detail="Source or target connection not found")

    svc = get_job_service()

    async def _do(append_log) -> dict:
        append_log(f"Executing plan {plan.name} ({plan.id})")
        # For clean Task 4, we just simulate execution via preview counts
        # Full execution would call MigrationCoordinator per phase; we keep lightweight

        # Flatten phases to resource types
        phases: list[dict] = json.loads(plan.phases_json) if plan.phases_json else []
        all_rtypes: list[str] = []
        for ph in phases:
            all_rtypes.extend(ph.get("resource_types", []))
        if not all_rtypes:
            all_rtypes = ["organizations"]
        append_log(f"Plan resource types: {all_rtypes}")
        # Simulate counts via export service quickly
        # We won't actually call AAP, just echo
        append_log("Plan execution finished (simulated)")
        return {
            "plan_id": plan.id,
            "status": "completed",
            "phases": len(phases),
            "resource_types": all_rtypes,
        }

    job = await svc.start_job("plan_execute", _do, name=f"plan:{plan.name}")
    # Update plan status
    plan.status = "running"
    db.commit()
    return {"job_id": job.job_id, "seq_id": job.seq_id, "plan_id": plan.id, "status": "running"}
