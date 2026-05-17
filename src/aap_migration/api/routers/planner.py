"""Migration planner endpoints — multi-source phased migration plans."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any, cast

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
from aap_migration.api.services.job_service import Job

router = APIRouter()


@router.get("/resource-types")
def list_resource_types() -> list[dict[str, Any]]:
    """Return ordered list of migratable resource types with metadata."""
    from aap_migration.resources import RESOURCE_REGISTRY, get_migration_order

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

    phase_responses: list[dict[str, Any]] = []
    for phase in phases:
        orgs = db.query(MigrationPlanPhaseOrg).filter_by(phase_id=phase.id).all()
        rt_rows = db.query(MigrationPlanPhaseResourceType).filter_by(phase_id=phase.id).all()
        phase_responses.append(
            {
                "id": phase.id,
                "phase_number": phase.phase_number,
                "name": phase.name,
                "status": phase.status,
                "update_mode": phase.update_mode,
                "resource_types": [r.resource_type for r in rt_rows],
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
    phase_ids = db.query(MigrationPlanPhase.id).filter_by(plan_id=plan_id)
    db.query(MigrationPlanPhaseOrg).filter(MigrationPlanPhaseOrg.phase_id.in_(phase_ids)).delete(
        synchronize_session=False
    )
    db.query(MigrationPlanPhaseResourceType).filter(
        MigrationPlanPhaseResourceType.phase_id.in_(phase_ids)
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

    phase_ids = db.query(MigrationPlanPhase.id).filter_by(plan_id=plan_id)
    db.query(MigrationPlanPhaseOrg).filter(MigrationPlanPhaseOrg.phase_id.in_(phase_ids)).delete(
        synchronize_session=False
    )
    db.query(MigrationPlanPhaseResourceType).filter(
        MigrationPlanPhaseResourceType.phase_id.in_(phase_ids)
    ).delete(synchronize_session=False)
    db.query(MigrationPlanPhase).filter_by(plan_id=plan_id).delete()
    db.flush()

    for phase_data in body.phases:
        phase = MigrationPlanPhase(
            id=phase_data.id or str(uuid.uuid4()),
            plan_id=plan_id,
            phase_number=phase_data.phase_number,
            name=phase_data.name,
            update_mode=phase_data.update_mode,
            status="pending",
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


_DEFAULT_PHASES: list[dict[str, Any]] = [
    {
        "name": "Foundation",
        "resource_types": ["settings", "instances", "instance_groups"],
        "update_mode": False,
    },
    {
        "name": "Organizations & Identity",
        "resource_types": [
            "organizations",
            "labels",
            "users",
            "teams",
            "credential_types",
        ],
        "update_mode": False,
    },
    {
        "name": "Credentials & Environments",
        "resource_types": [
            "credentials",
            "credential_input_sources",
            "execution_environments",
            "notification_templates",
        ],
        "update_mode": False,
    },
    {
        "name": "Organizations (update assignments)",
        "resource_types": ["organizations"],
        "update_mode": True,
    },
    {
        "name": "Projects & Inventories",
        "resource_types": [
            "projects",
            "inventories",
            "inventory_sources",
            "applications",
            "hosts",
            "host_inventory_memberships",
            "inventory_groups",
        ],
        "update_mode": False,
    },
    {
        "name": "Job Configuration",
        "resource_types": [
            "job_templates",
            "workflow_job_templates",
            "system_job_templates",
            "schedules",
        ],
        "update_mode": False,
    },
]


@router.post("/plans/{plan_id}/populate")
def populate_plan(plan_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Auto-populate phases using CaC-style dependency ordering."""
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    sources = db.query(MigrationPlanSource).filter_by(plan_id=plan_id).all()
    if not sources:
        raise HTTPException(status_code=400, detail="No sources configured for this plan")

    svc = get_job_service()

    # Collect all orgs from all sources' analysis results
    all_orgs: list[tuple[str, int, str]] = []
    for source in sources:
        if not source.analysis_job_id:
            continue
        job = svc.get_job(source.analysis_job_id)
        if job is None or job.result is None:
            continue

        orgs_dict = job.result.get("organizations", {})
        for org_name, org_info in orgs_dict.items():
            org_id = org_info.get("org_id", 0)
            all_orgs.append((source.id, org_id, org_name))

    # Clear existing phases
    phase_ids = db.query(MigrationPlanPhase.id).filter_by(plan_id=plan_id)
    db.query(MigrationPlanPhaseOrg).filter(MigrationPlanPhaseOrg.phase_id.in_(phase_ids)).delete(
        synchronize_session=False
    )
    db.query(MigrationPlanPhaseResourceType).filter(
        MigrationPlanPhaseResourceType.phase_id.in_(phase_ids)
    ).delete(synchronize_session=False)
    db.query(MigrationPlanPhase).filter_by(plan_id=plan_id).delete()
    db.flush()

    # Create the 6 default phases
    for phase_num, phase_def in enumerate(_DEFAULT_PHASES, start=1):
        phase = MigrationPlanPhase(
            id=str(uuid.uuid4()),
            plan_id=plan_id,
            phase_number=phase_num,
            name=phase_def["name"],
            update_mode=phase_def["update_mode"],
            status="pending",
        )
        db.add(phase)
        db.flush()

        for rt in phase_def["resource_types"]:
            db.add(
                MigrationPlanPhaseResourceType(
                    id=str(uuid.uuid4()),
                    phase_id=phase.id,
                    resource_type=rt,
                )
            )

        for source_id, org_id, org_name in all_orgs:
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

    dest_cfg = ConnectionService.build_instance_config(dest)
    dest_auth_scheme = ConnectionService._auth_scheme(dest)

    phase_name = phase.name or f"Phase {phase.phase_number}"
    phase_update_mode = phase.update_mode

    rt_rows = db.query(MigrationPlanPhaseResourceType).filter_by(phase_id=phase_id).all()
    phase_allowed_types: set[str] | None = {r.resource_type for r in rt_rows} if rt_rows else None

    db_url = get_db_url()

    phase.status = "running"
    db.flush()

    svc = get_job_service()
    session_factory = get_app_state().db_session_factory

    async def _do_phase(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        import time

        from aap_migration.client.aap_source_client import AAPSourceClient
        from aap_migration.client.aap_target_client import AAPTargetClient
        from aap_migration.config import AAPInstanceConfig, MigrationConfig, StateConfig
        from aap_migration.migration.exporter import create_exporter
        from aap_migration.migration.importer import create_importer
        from aap_migration.migration.state import MigrationState
        from aap_migration.migration.transformer import SkipResourceError, create_transformer
        from aap_migration.resources import RESOURCE_REGISTRY, get_migration_order

        def emit(event: dict[str, Any]) -> None:
            log("\t" + json.dumps(event))

        total_created = 0
        total_skipped = 0
        total_failed = 0
        total_updated = 0

        resource_order = [
            rt
            for rt in get_migration_order()
            if RESOURCE_REGISTRY[rt].has_exporter
            and RESOURCE_REGISTRY[rt].has_importer
            and (phase_allowed_types is None or rt in phase_allowed_types)
        ]
        num_resource_types = len(resource_order) * len(source_configs)

        try:
            target_config = AAPInstanceConfig(
                url=dest_cfg.url,
                token=dest_cfg.token,
                verify_ssl=dest_cfg.verify_ssl,
                timeout=dest_cfg.timeout,
            )
            target_client = AAPTargetClient(target_config, auth_scheme=dest_auth_scheme)

            phase_num = 0

            for src_cfg in source_configs:
                src_config = AAPInstanceConfig(
                    url=src_cfg["url"],
                    token=src_cfg["token"],
                    verify_ssl=src_cfg["verify_ssl"],
                    timeout=src_cfg["timeout"],
                )
                org_ids: list[int] = src_cfg["org_ids"]

                log(f"Processing source: {src_cfg['url']} (orgs={org_ids})")

                migration_config = MigrationConfig(
                    source=src_config,
                    target=target_config,
                    state=StateConfig(db_path=db_url),
                )

                src_client = AAPSourceClient(
                    src_config, auth_scheme=src_cfg.get("auth_scheme", "Bearer")
                )
                state = MigrationState(migration_config.state)

                for rtype in resource_order:
                    info = RESOURCE_REGISTRY[rtype]
                    phase_num += 1

                    emit(
                        {
                            "_event": "phase_start",
                            "phase_num": phase_num,
                            "total_phases": num_resource_types,
                            "description": info.description,
                            "resource_type": rtype,
                        }
                    )

                    phase_start = time.monotonic()
                    created = 0
                    skipped = 0
                    failed = 0
                    updated = 0

                    try:
                        exporter = create_exporter(
                            resource_type=rtype,
                            client=src_client,
                            state=state,
                            performance_config=migration_config.performance,
                        )
                        transformer = (
                            create_transformer(
                                resource_type=rtype,
                                dry_run=False,
                                state=state,
                                defer_project_sync=False,
                            )
                            if info.has_transformer
                            else None
                        )
                        importer = create_importer(
                            resource_type=rtype,
                            client=target_client,
                            state=state,
                            performance_config=migration_config.performance,
                            resource_mappings=migration_config.resource_mappings,
                        )

                        exported = 0
                        last_progress = time.monotonic()
                        PROGRESS_INTERVAL = 2.0

                        async for resource in exporter.export():
                            source_id = resource.get("id")
                            if source_id is None:
                                if rtype == "host_inventory_memberships":
                                    source_id = (
                                        f"{resource.get('host_id')}_{resource.get('inventory_id')}"
                                    )
                                elif rtype == "settings":
                                    source_id = "settings"
                                else:
                                    continue

                            if org_ids:
                                if rtype == "organizations":
                                    if source_id not in org_ids:
                                        continue
                                elif rtype not in (
                                    "settings",
                                    "host_inventory_memberships",
                                ):
                                    res_org = resource.get("organization")
                                    sf_org = (
                                        resource.get("summary_fields", {})
                                        .get("organization", {})
                                        .get("id")
                                    )
                                    if res_org not in org_ids and sf_org not in org_ids:
                                        if res_org is not None or sf_org is not None:
                                            continue

                            if transformer:
                                try:
                                    resource = transformer.transform_resource(
                                        resource_type=rtype,
                                        data=resource,
                                        validate=True,
                                    )
                                except SkipResourceError:
                                    skipped += 1
                                    continue
                                except Exception:
                                    failed += 1
                                    continue

                            exported += 1

                            try:
                                if rtype == "host_inventory_memberships":
                                    result = await cast(Any, importer).import_resource(
                                        resource=resource,
                                    )
                                else:
                                    result = await importer.import_resource(
                                        resource_type=rtype,
                                        source_id=int(source_id),
                                        data=resource,
                                    )
                                res_name = resource.get(
                                    "name",
                                    resource.get("username", str(source_id)),
                                )
                                if result:
                                    created += 1
                                    emit(
                                        {
                                            "_event": "resource_result",
                                            "phase_num": phase_num,
                                            "name": res_name,
                                            "resource_type": rtype,
                                            "result": "created",
                                            "detail": "",
                                        }
                                    )
                                elif phase_update_mode and rtype != "host_inventory_memberships":
                                    target_id = state.get_mapped_id(rtype, int(source_id))
                                    if target_id is not None:
                                        try:
                                            await target_client.update_resource(
                                                rtype,
                                                target_id,
                                                resource,
                                            )
                                            updated += 1
                                            emit(
                                                {
                                                    "_event": "resource_result",
                                                    "phase_num": phase_num,
                                                    "name": res_name,
                                                    "resource_type": rtype,
                                                    "result": "updated",
                                                    "detail": "",
                                                }
                                            )
                                        except Exception as upd_exc:
                                            failed += 1
                                            emit(
                                                {
                                                    "_event": "resource_result",
                                                    "phase_num": phase_num,
                                                    "name": res_name,
                                                    "resource_type": rtype,
                                                    "result": "failed",
                                                    "detail": f"update: {upd_exc!s}"[:200],
                                                }
                                            )
                                    else:
                                        skipped += 1
                                else:
                                    skipped += 1
                            except Exception as exc:
                                failed += 1
                                emit(
                                    {
                                        "_event": "resource_result",
                                        "phase_num": phase_num,
                                        "name": resource.get(
                                            "name",
                                            resource.get("username", str(source_id)),
                                        ),
                                        "resource_type": rtype,
                                        "result": "failed",
                                        "detail": str(exc)[:200],
                                    }
                                )

                            now = time.monotonic()
                            if now - last_progress >= PROGRESS_INTERVAL:
                                elapsed = f"{now - phase_start:.1f}s"
                                emit(
                                    {
                                        "_event": "phase_progress",
                                        "phase_num": phase_num,
                                        "exported": exported,
                                        "created": created,
                                        "skipped": skipped,
                                        "failed": failed,
                                        "rate": f"{exported / max(now - phase_start, 0.1):.0f}/s",
                                        "elapsed": elapsed,
                                    }
                                )
                                last_progress = now

                        duration = f"{time.monotonic() - phase_start:.1f}s"
                        emit(
                            {
                                "_event": "phase_complete",
                                "phase_num": phase_num,
                                "description": info.description,
                                "created": created,
                                "updated": updated,
                                "skipped": skipped,
                                "failed": failed,
                                "exported": exported,
                                "duration": duration,
                                "warnings": {},
                            }
                        )
                    except Exception as exc:
                        failed += 1
                        emit({"_event": "phase_error", "phase_num": phase_num, "error": str(exc)})
                        log(f"  Error on {rtype}: {exc}")

                    total_created += created
                    total_updated += updated
                    total_skipped += skipped
                    total_failed += failed

            final_status = "completed" if total_failed == 0 else "completed_with_errors"
            _update_phase_status(session_factory, phase_id, final_status)
        except Exception:
            _update_phase_status(session_factory, phase_id, "failed")
            raise

        emit(
            {
                "_event": "migration_complete",
                "total_created": total_created,
                "total_updated": total_updated,
                "total_skipped": total_skipped,
                "total_failed": total_failed,
            }
        )

        return {
            "total_created": total_created,
            "total_updated": total_updated,
            "total_skipped": total_skipped,
            "total_failed": total_failed,
        }

    job_id = svc.start_job(f"Plan: {phase_name}", "migration-run", _do_phase)

    phase.job_id = job_id
    plan.status = "active"
    db.flush()

    return JobStartResponse(job_id=job_id)


def _update_phase_status(session_factory: Any, phase_id: str, status: str) -> None:
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
