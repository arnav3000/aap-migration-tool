"""Migration planner endpoints — multi-source phased migration plans."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_app_state, get_db, get_db_url, get_job_service
from aap_migration.api.models import (
    MigrationPlan,
    MigrationPlanPhase,
    MigrationPlanPhaseOrg,
    MigrationPlanPhaseResourceType,
    MigrationPlanSource,
)
from aap_migration.api.schemas import (
    JobStartResponse,
    PhasesUpdateRequest,
    PlanCreate,
    PlanUpdate,
)
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.job_service import Job, PhaseStatus
from aap_migration.migration.runner import (
    _build_source_contexts,
    _handle_credential_pause,
    _migrate_resource_type,
    _run_cac_org_update,
)

router = APIRouter()


def _clear_plan_phases(db: Session, plan_id: str) -> None:
    """Delete all phases, phase orgs, and phase resource types for a plan."""
    phase_ids = db.query(MigrationPlanPhase.id).filter_by(plan_id=plan_id)
    db.query(MigrationPlanPhaseOrg).filter(MigrationPlanPhaseOrg.phase_id.in_(phase_ids)).delete(
        synchronize_session=False
    )
    db.query(MigrationPlanPhaseResourceType).filter(
        MigrationPlanPhaseResourceType.phase_id.in_(phase_ids)
    ).delete(synchronize_session=False)
    db.query(MigrationPlanPhase).filter_by(plan_id=plan_id).delete()


def _get_importer_deps() -> dict[str, list[str]]:
    """Build resource_type -> list of dependency resource types from importer classes."""
    from aap_migration.migration.importer import IMPORTER_REGISTRY

    result: dict[str, list[str]] = {}
    for rtype, cls in IMPORTER_REGISTRY.items():
        dep_dict: dict[str, str] = getattr(cls, "DEPENDENCIES", {}) or {}
        deps = sorted(set(dep_dict.values())) if dep_dict else []
        result[rtype] = deps
    return result


@router.get("/resource-types")
def list_resource_types() -> list[dict[str, Any]]:
    """Return ordered list of migratable resource types with metadata and dependencies."""
    from aap_migration.resources import RESOURCE_REGISTRY, get_migration_order

    importer_deps = _get_importer_deps()

    result = []
    for rtype in get_migration_order():
        info = RESOURCE_REGISTRY[rtype]
        if not info.has_exporter or not info.has_importer:
            continue
        result.append(
            {
                "name": rtype,
                "description": info.description,
                "migration_order": info.migration_order,
                "dependencies": importer_deps.get(rtype, []),
            }
        )
    return result


def _build_plan_response(db: Session, plan: MigrationPlan) -> dict[str, Any]:
    """Build a full plan response dict with sources and phases."""
    sources = db.query(MigrationPlanSource).filter_by(plan_id=plan.id).all()
    phases = (
        db.query(MigrationPlanPhase)
        .filter_by(plan_id=plan.id)
        .order_by(MigrationPlanPhase.phase_number)
        .all()
    )
    phase_ids = [ph.id for ph in phases]

    all_orgs = (
        db.query(MigrationPlanPhaseOrg).filter(MigrationPlanPhaseOrg.phase_id.in_(phase_ids)).all()
        if phase_ids
        else []
    )
    all_rts = (
        db.query(MigrationPlanPhaseResourceType)
        .filter(MigrationPlanPhaseResourceType.phase_id.in_(phase_ids))
        .all()
        if phase_ids
        else []
    )

    orgs_by_phase: dict[str, list[MigrationPlanPhaseOrg]] = {pid: [] for pid in phase_ids}
    for o in all_orgs:
        orgs_by_phase[o.phase_id].append(o)
    rts_by_phase: dict[str, list[MigrationPlanPhaseResourceType]] = {pid: [] for pid in phase_ids}
    for r in all_rts:
        rts_by_phase[r.phase_id].append(r)

    phase_responses: list[dict[str, Any]] = []
    for phase in phases:
        phase_responses.append(
            {
                "id": phase.id,
                "phase_number": phase.phase_number,
                "name": phase.name,
                "status": phase.status,
                "update_mode": phase.update_mode,
                "resource_types": [r.resource_type for r in rts_by_phase[phase.id]],
                "job_id": phase.job_id,
                "orgs": [
                    {
                        "id": o.id,
                        "source_id": o.source_id,
                        "org_id": o.org_id,
                        "org_name": o.org_name,
                    }
                    for o in orgs_by_phase[phase.id]
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
    from sqlalchemy import func

    plans = db.query(MigrationPlan).order_by(MigrationPlan.updated_at.desc()).all()
    plan_ids = [p.id for p in plans]

    source_counts: dict[str, int] = {}
    phase_counts: dict[str, int] = {}
    if plan_ids:
        source_counts = {
            row[0]: row[1]
            for row in db.query(MigrationPlanSource.plan_id, func.count())
            .filter(MigrationPlanSource.plan_id.in_(plan_ids))
            .group_by(MigrationPlanSource.plan_id)
            .all()
        }
        phase_counts = {
            row[0]: row[1]
            for row in db.query(MigrationPlanPhase.plan_id, func.count())
            .filter(MigrationPlanPhase.plan_id.in_(plan_ids))
            .group_by(MigrationPlanPhase.plan_id)
            .all()
        }

    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "status": p.status,
            "destination_id": p.destination_id,
            "created_at": p.created_at.isoformat() if p.created_at else "",
            "updated_at": p.updated_at.isoformat() if p.updated_at else "",
            "source_count": source_counts.get(p.id, 0),
            "phase_count": phase_counts.get(p.id, 0),
        }
        for p in plans
    ]


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
    _clear_plan_phases(db, plan_id)
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

    _clear_plan_phases(db, plan_id)
    db.flush()

    for phase_data in body.phases:
        phase = MigrationPlanPhase(
            id=phase_data.id or str(uuid.uuid4()),
            plan_id=plan_id,
            phase_number=phase_data.phase_number,
            name=phase_data.name,
            update_mode=phase_data.update_mode,
            status=PhaseStatus.PENDING,
        )
        db.add(phase)
        db.flush()

        if phase_data.resource_types:
            for rt in phase_data.resource_types:
                db.add(
                    MigrationPlanPhaseResourceType(
                        id=str(uuid.uuid4()),
                        phase_id=phase.id,
                        resource_type=rt,
                    )
                )

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
    """Auto-populate phases — one phase per analysis wave."""
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    sources = db.query(MigrationPlanSource).filter_by(plan_id=plan_id).all()
    if not sources:
        raise HTTPException(status_code=400, detail="No sources configured for this plan")

    svc = get_job_service()

    waves: dict[int, list[tuple[str, int, str]]] = {}

    for source in sources:
        if not source.analysis_job_id:
            continue
        job = svc.get_job(source.analysis_job_id)
        if job is None or job.result is None:
            continue

        orgs_dict = job.result.get("organizations", {})
        migration_phases = job.result.get("migration_phases", [])

        org_wave: dict[str, int] = {}
        for phase_data in migration_phases:
            wave_num = phase_data.get("phase", 1)
            raw_orgs = phase_data.get("orgs", [])
            if isinstance(raw_orgs, dict) and "orgs" in raw_orgs:
                raw_orgs = raw_orgs["orgs"]
            for org_name in raw_orgs if isinstance(raw_orgs, list) else []:
                org_wave[org_name] = wave_num

        for org_name, org_info in orgs_dict.items():
            org_id = org_info.get("org_id", 0)
            wave = org_wave.get(org_name, 1)
            entry = (source.id, org_id, org_name)
            waves.setdefault(wave, []).append(entry)

    _clear_plan_phases(db, plan_id)
    db.flush()

    num_waves = len(waves)
    use_wave_prefix = num_waves > 1

    for wave_num in sorted(waves.keys()):
        wave_orgs = waves[wave_num]
        name = f"Wave {wave_num}" if use_wave_prefix else f"Phase {wave_num}"
        phase = MigrationPlanPhase(
            id=str(uuid.uuid4()),
            plan_id=plan_id,
            phase_number=wave_num,
            name=f"{name} ({len(wave_orgs)} orgs)",
            update_mode=False,
            status=PhaseStatus.PENDING,
        )
        db.add(phase)
        db.flush()
        for source_id, org_id, org_name in wave_orgs:
            db.add(
                MigrationPlanPhaseOrg(
                    id=str(uuid.uuid4()),
                    phase_id=phase.id,
                    source_id=source_id,
                    org_id=org_id,
                    org_name=org_name,
                )
            )

    db.flush()
    return _build_plan_response(db, plan)


# ---------------------------------------------------------------------------
# execute_phase endpoint
# ---------------------------------------------------------------------------


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
                "connection_name": getattr(conn, "name", None) or cfg.url,
                "org_ids": org_ids,
                "auth_scheme": ConnectionService._auth_scheme(conn),
                "source_key": ps.connection_id or ps.id,
                "connection_id": ps.connection_id,
            }
        )

    dest_cfg = ConnectionService.build_instance_config(dest)
    dest_auth_scheme = ConnectionService._auth_scheme(dest)

    phase_name = phase.name or f"Phase {phase.phase_number}"
    db_url = get_db_url()

    # Optional per-phase resource-type filter (empty = all fully supported types)
    phase_resource_types = [
        row.resource_type
        for row in db.query(MigrationPlanPhaseResourceType).filter_by(phase_id=phase_id).all()
    ]

    svc = get_job_service()
    session_factory = get_app_state().db_session_factory

    async def _do_phase(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        from aap_migration.resources import RESOURCE_REGISTRY, get_fully_supported_types

        def emit(event: dict[str, Any]) -> None:
            log("\t" + json.dumps(event))

        totals = {"created": 0, "skipped": 0, "failed": 0, "updated": 0}

        resource_order = get_fully_supported_types()
        if phase_resource_types:
            allowed = set(phase_resource_types)
            resource_order = [r for r in resource_order if r in allowed]
            if not resource_order:
                raise ValueError(
                    "Phase resource_types filter excluded every migratable type — "
                    "check the phase configuration"
                )
        from aap_migration.resources import apply_host_membership_resource_cascade

        resource_order = apply_host_membership_resource_cascade(resource_order)
        if not resource_order:
            raise ValueError(
                "Phase resource_types filter excluded every migratable type — "
                "check the phase configuration"
            )
        num_resource_types = len(resource_order)
        CRED_PAUSE_AFTER = {"credentials", "credential_input_sources"}
        created_creds: list[dict[str, str]] = []

        try:
            _, target_client, _sources = _build_source_contexts(
                source_configs,
                dest_cfg,
                dest_auth_scheme,
                db_url,
            )

            for phase_num, rtype in enumerate(resource_order, 1):
                emit(
                    {
                        "_event": "phase_start",
                        "phase_num": phase_num,
                        "total_phases": num_resource_types,
                        "description": RESOURCE_REGISTRY[rtype].description,
                        "resource_type": rtype,
                    }
                )

                created, skipped, failed, exported = await _migrate_resource_type(
                    rtype,
                    _sources,
                    target_client,
                    phase_num,
                    emit,
                    log,
                    created_creds,
                )
                totals["created"] += created
                totals["skipped"] += skipped
                totals["failed"] += failed

                if rtype in CRED_PAUSE_AFTER:
                    remaining = CRED_PAUSE_AFTER - set(
                        resource_order[: resource_order.index(rtype) + 1]
                    )
                    if not remaining and created_creds:
                        await _handle_credential_pause(
                            job,
                            svc,
                            created_creds,
                            _sources,
                            plan_id,
                            phase_id,
                            emit,
                            log,
                        )

            log("CaC pass: re-patching organizations with final references...")
            totals["updated"] += await _run_cac_org_update(
                _sources,
                target_client,
                len(resource_order),
                emit,
                log,
            )

            final_status = (
                PhaseStatus.COMPLETED
                if totals["failed"] == 0
                else PhaseStatus.COMPLETED_WITH_ERRORS
            )
            _update_phase_status(session_factory, phase_id, final_status)
        except Exception:
            _update_phase_status(session_factory, phase_id, PhaseStatus.FAILED)
            raise

        emit(
            {
                "_event": "migration_complete",
                "total_created": totals["created"],
                "total_updated": totals["updated"],
                "total_skipped": totals["skipped"],
                "total_failed": totals["failed"],
            }
        )
        return totals

    # Persist api_jobs via start_job BEFORE any write on this request session.
    # Flushing phase.status first holds SQLite's write lock, so the separate
    # persist session hits "database is locked", swallows it, and the later
    # phase.job_id FK update fails with FOREIGN KEY constraint failed.
    job_id = svc.start_job(f"Plan: {phase_name}", "migration-run", _do_phase)

    phase.status = PhaseStatus.RUNNING
    phase.job_id = job_id
    plan.status = "active"
    db.flush()

    return JobStartResponse(job_id=job_id)


def _update_phase_status(session_factory: Any, phase_id: str, status: PhaseStatus) -> None:
    """Update the phase status in the DB from the background task."""
    import logging

    logger = logging.getLogger(__name__)
    session = session_factory()
    try:
        phase = session.get(MigrationPlanPhase, phase_id)
        if phase is not None:
            phase.status = status
        session.commit()
    except Exception:
        logger.exception("Failed to update phase %s to status %s", phase_id, status)
        session.rollback()
    finally:
        session.close()
