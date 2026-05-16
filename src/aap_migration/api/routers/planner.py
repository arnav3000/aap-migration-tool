"""Migration planner endpoints — multi-source phased migration plans."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, get_job_service
from aap_migration.api.models import (
    MigrationPlan,
    MigrationPlanPhase,
    MigrationPlanPhaseOrg,
    MigrationPlanSource,
)
from aap_migration.api.schemas import (
    JobStartResponse,
    PhasesUpdateRequest,
    PlanCreate,
    PlanUpdate,
)
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.job_service import Job

router = APIRouter()


def _build_plan_response(db: Session, plan: MigrationPlan) -> dict[str, Any]:
    """Build a full plan response dict with sources and phases."""
    sources = db.query(MigrationPlanSource).filter_by(plan_id=plan.id).all()
    phases = (
        db.query(MigrationPlanPhase)
        .filter_by(plan_id=plan.id)
        .order_by(MigrationPlanPhase.phase_number)
        .all()
    )

    phase_responses: list[dict[str, Any]] = []
    for phase in phases:
        orgs = db.query(MigrationPlanPhaseOrg).filter_by(phase_id=phase.id).all()
        phase_responses.append(
            {
                "id": phase.id,
                "phase_number": phase.phase_number,
                "name": phase.name,
                "status": phase.status,
                "job_id": phase.job_id,
                "orgs": [
                    {
                        "id": o.id,
                        "source_id": o.source_id,
                        "org_id": o.org_id,
                        "org_name": o.org_name,
                    }
                    for o in orgs
                ],
            }
        )

    return {
        "id": plan.id,
        "name": plan.name,
        "description": plan.description,
        "status": plan.status,
        "destination_id": plan.destination_id,
        "created_at": plan.created_at.isoformat() if plan.created_at else "",
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else "",
        "sources": [
            {
                "id": s.id,
                "connection_id": s.connection_id,
                "name_prefix": s.name_prefix,
                "analysis_job_id": s.analysis_job_id,
            }
            for s in sources
        ],
        "phases": phase_responses,
    }


@router.post("/plans")
def create_plan(body: PlanCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    plan = MigrationPlan(
        id=str(uuid.uuid4()),
        name=body.name,
        description=body.description,
        status="draft",
        destination_id=body.destination_id,
    )
    db.add(plan)

    for src in body.sources:
        ps = MigrationPlanSource(
            id=str(uuid.uuid4()),
            plan_id=plan.id,
            connection_id=src.connection_id,
            name_prefix=src.name_prefix,
            analysis_job_id=src.analysis_job_id,
        )
        db.add(ps)

    db.flush()
    return _build_plan_response(db, plan)


@router.get("/plans")
def list_plans(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    plans = db.query(MigrationPlan).order_by(MigrationPlan.updated_at.desc()).all()
    result: list[dict[str, Any]] = []
    for p in plans:
        source_count = db.query(MigrationPlanSource).filter_by(plan_id=p.id).count()
        phase_count = db.query(MigrationPlanPhase).filter_by(plan_id=p.id).count()
        result.append(
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "status": p.status,
                "destination_id": p.destination_id,
                "created_at": p.created_at.isoformat() if p.created_at else "",
                "updated_at": p.updated_at.isoformat() if p.updated_at else "",
                "source_count": source_count,
                "phase_count": phase_count,
            }
        )
    return result


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return _build_plan_response(db, plan)


@router.put("/plans/{plan_id}")
def update_plan(plan_id: str, body: PlanUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    if body.name is not None:
        plan.name = body.name
    if body.description is not None:
        plan.description = body.description
    if body.destination_id is not None:
        plan.destination_id = body.destination_id
    if body.status is not None:
        plan.status = body.status

    db.flush()
    return _build_plan_response(db, plan)


@router.delete("/plans/{plan_id}", status_code=204)
def delete_plan(plan_id: str, db: Session = Depends(get_db)) -> None:
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    db.query(MigrationPlanPhaseOrg).filter(
        MigrationPlanPhaseOrg.phase_id.in_(
            db.query(MigrationPlanPhase.id).filter_by(plan_id=plan_id)
        )
    ).delete(synchronize_session=False)
    db.query(MigrationPlanPhase).filter_by(plan_id=plan_id).delete()
    db.query(MigrationPlanSource).filter_by(plan_id=plan_id).delete()
    db.delete(plan)


@router.put("/plans/{plan_id}/phases")
def update_phases(
    plan_id: str, body: PhasesUpdateRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    if body.sources is not None:
        db.query(MigrationPlanSource).filter_by(plan_id=plan_id).delete()
        for src in body.sources:
            ps = MigrationPlanSource(
                id=src.id or str(uuid.uuid4()),
                plan_id=plan_id,
                connection_id=src.connection_id,
                name_prefix=src.name_prefix,
                analysis_job_id=src.analysis_job_id,
            )
            db.add(ps)
        db.flush()

    db.query(MigrationPlanPhaseOrg).filter(
        MigrationPlanPhaseOrg.phase_id.in_(
            db.query(MigrationPlanPhase.id).filter_by(plan_id=plan_id)
        )
    ).delete(synchronize_session=False)
    db.query(MigrationPlanPhase).filter_by(plan_id=plan_id).delete()
    db.flush()

    for phase_data in body.phases:
        phase = MigrationPlanPhase(
            id=phase_data.id or str(uuid.uuid4()),
            plan_id=plan_id,
            phase_number=phase_data.phase_number,
            name=phase_data.name,
            status="pending",
        )
        db.add(phase)
        db.flush()

        for org in phase_data.orgs:
            po = MigrationPlanPhaseOrg(
                id=str(uuid.uuid4()),
                phase_id=phase.id,
                source_id=org.source_id,
                org_id=org.org_id,
                org_name=org.org_name,
            )
            db.add(po)

    db.flush()
    return _build_plan_response(db, plan)


@router.post("/plans/{plan_id}/populate")
def populate_plan(plan_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Auto-populate phases from analysis results for all sources in the plan."""
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    sources = db.query(MigrationPlanSource).filter_by(plan_id=plan_id).all()
    if not sources:
        raise HTTPException(status_code=400, detail="No sources configured for this plan")

    svc = get_job_service()

    db.query(MigrationPlanPhaseOrg).filter(
        MigrationPlanPhaseOrg.phase_id.in_(
            db.query(MigrationPlanPhase.id).filter_by(plan_id=plan_id)
        )
    ).delete(synchronize_session=False)
    db.query(MigrationPlanPhase).filter_by(plan_id=plan_id).delete()
    db.flush()

    all_phases: dict[int, list[tuple[str, int, str]]] = {}

    for source in sources:
        if not source.analysis_job_id:
            continue
        job = svc.get_job(source.analysis_job_id)
        if job is None or job.result is None:
            continue

        migration_phases = job.result.get("migration_phases", [])
        for phase_data in migration_phases:
            phase_num = phase_data.get("phase", 1)
            raw_orgs = phase_data.get("orgs", [])
            if isinstance(raw_orgs, dict) and "orgs" in raw_orgs:
                raw_orgs = raw_orgs["orgs"]
            org_names = raw_orgs if isinstance(raw_orgs, list) else []

            orgs_dict = job.result.get("organizations", {})
            for org_name in org_names:
                org_info = orgs_dict.get(org_name, {})
                org_id = org_info.get("org_id", 0)
                if phase_num not in all_phases:
                    all_phases[phase_num] = []
                all_phases[phase_num].append((source.id, org_id, org_name))

    for phase_num in sorted(all_phases.keys()):
        phase = MigrationPlanPhase(
            id=str(uuid.uuid4()),
            plan_id=plan_id,
            phase_number=phase_num,
            name=f"Phase {phase_num}",
            status="pending",
        )
        db.add(phase)
        db.flush()

        for source_id, org_id, org_name in all_phases[phase_num]:
            po = MigrationPlanPhaseOrg(
                id=str(uuid.uuid4()),
                phase_id=phase.id,
                source_id=source_id,
                org_id=org_id,
                org_name=org_name,
            )
            db.add(po)

    db.flush()
    return _build_plan_response(db, plan)


@router.post("/plans/{plan_id}/phases/{phase_id}/execute", response_model=JobStartResponse)
async def execute_phase(
    plan_id: str, phase_id: str, db: Session = Depends(get_db)
) -> JobStartResponse:
    """Execute a single phase of the plan."""
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    phase = db.get(MigrationPlanPhase, phase_id)
    if phase is None or phase.plan_id != plan_id:
        raise HTTPException(status_code=404, detail="Phase not found")

    if not plan.destination_id:
        raise HTTPException(status_code=400, detail="Plan has no destination configured")

    dest = ConnectionService.get(db, plan.destination_id)
    if dest is None:
        raise HTTPException(status_code=404, detail="Destination connection not found")

    orgs = db.query(MigrationPlanPhaseOrg).filter_by(phase_id=phase_id).all()
    if not orgs:
        raise HTTPException(status_code=400, detail="Phase has no organizations")

    sources = db.query(MigrationPlanSource).filter_by(plan_id=plan_id).all()
    source_map: dict[str, MigrationPlanSource] = {s.id: s for s in sources}

    orgs_by_source: dict[str, list[int]] = {}
    for org in orgs:
        orgs_by_source.setdefault(org.source_id, []).append(org.org_id)

    source_configs: list[dict[str, Any]] = []
    for source_id, org_ids in orgs_by_source.items():
        ps = source_map.get(source_id)
        if ps is None:
            continue
        conn = ConnectionService.get(db, ps.connection_id)
        if conn is None:
            continue
        cfg = ConnectionService.build_instance_config(conn)
        source_configs.append(
            {
                "url": cfg.url,
                "token": cfg.token,
                "verify_ssl": cfg.verify_ssl,
                "timeout": cfg.timeout,
                "name_prefix": ps.name_prefix or "",
                "org_ids": org_ids,
                "auth_scheme": ConnectionService._auth_scheme(conn),
            }
        )

    phase_name = phase.name or f"Phase {phase.phase_number}"

    phase.status = "running"
    db.flush()

    svc = get_job_service()

    async def _do_phase(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        import time

        from aap_migration.client.aap_source_client import AAPSourceClient
        from aap_migration.config import AAPInstanceConfig
        from aap_migration.resources import RESOURCE_REGISTRY, get_exportable_types

        def emit(event: dict[str, Any]) -> None:
            log("\t" + json.dumps(event))

        resource_types = get_exportable_types()
        total_created = 0
        total_skipped = 0
        total_failed = 0
        phase_num = 0

        for src_cfg in source_configs:
            src_config = AAPInstanceConfig(
                url=src_cfg["url"],
                token=src_cfg["token"],
                verify_ssl=src_cfg["verify_ssl"],
                timeout=src_cfg["timeout"],
            )
            prefix = src_cfg["name_prefix"]
            org_ids = src_cfg["org_ids"]

            log(f"Processing source: {src_cfg['url']} (prefix='{prefix}', orgs={org_ids})")

            src_client = AAPSourceClient(
                src_config, auth_scheme=src_cfg.get("auth_scheme", "Bearer")
            )
            async with src_client:
                for rtype in resource_types:
                    info = RESOURCE_REGISTRY.get(rtype)
                    if not info:
                        continue

                    phase_num += 1
                    emit(
                        {
                            "_event": "phase_start",
                            "phase_num": phase_num,
                            "total_phases": len(resource_types) * len(source_configs),
                            "description": f"Export {info.description}",
                            "resource_type": rtype,
                        }
                    )

                    phase_start = time.monotonic()
                    created = 0
                    skipped = 0
                    failed = 0

                    try:
                        items = await src_client.get_paginated(info.endpoint, page_size=200)

                        if items and org_ids:
                            if rtype == "organizations":
                                items = [i for i in items if i.get("id") in org_ids]
                            else:
                                items = [
                                    i
                                    for i in items
                                    if i.get("organization") in org_ids
                                    or i.get("summary_fields", {}).get("organization", {}).get("id")
                                    in org_ids
                                ]

                        for item in items or []:
                            item_name = item.get(
                                "name", item.get("username", str(item.get("id", 0)))
                            )
                            if prefix and rtype != "users":
                                item_name = f"{prefix}{item_name}"
                            created += 1
                            emit(
                                {
                                    "_event": "resource_result",
                                    "phase_num": phase_num,
                                    "name": item_name,
                                    "resource_type": rtype,
                                    "result": "created",
                                    "detail": "Exported from source",
                                }
                            )

                        duration = f"{time.monotonic() - phase_start:.1f}s"
                        emit(
                            {
                                "_event": "phase_complete",
                                "phase_num": phase_num,
                                "description": f"Export {info.description}",
                                "created": created,
                                "skipped": skipped,
                                "failed": failed,
                                "exported": len(items or []),
                                "duration": duration,
                                "warnings": {},
                            }
                        )
                    except Exception as exc:
                        failed += 1
                        emit({"_event": "phase_error", "phase_num": phase_num, "error": str(exc)})
                        log(f"  Error on {rtype}: {exc}")

                    total_created += created
                    total_skipped += skipped
                    total_failed += failed

        emit(
            {
                "_event": "migration_complete",
                "total_created": total_created,
                "total_skipped": total_skipped,
                "total_failed": total_failed,
            }
        )
        return {
            "total_created": total_created,
            "total_skipped": total_skipped,
            "total_failed": total_failed,
        }

    job_id = svc.start_job(f"Plan: {phase_name}", "migration-run", _do_phase)

    phase.job_id = job_id
    plan.status = "active"
    db.flush()

    return JobStartResponse(job_id=job_id)
