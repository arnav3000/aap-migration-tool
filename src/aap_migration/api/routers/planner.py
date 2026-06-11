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
from aap_migration.api.services.job_service import Job, JobStatus, PhaseStatus

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
        source_counts = dict(
            db.query(MigrationPlanSource.plan_id, func.count())
            .filter(MigrationPlanSource.plan_id.in_(plan_ids))
            .group_by(MigrationPlanSource.plan_id)
            .all()
        )
        phase_counts = dict(
            db.query(MigrationPlanPhase.plan_id, func.count())
            .filter(MigrationPlanPhase.plan_id.in_(plan_ids))
            .group_by(MigrationPlanPhase.plan_id)
            .all()
        )

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


_CRED_CONSUMERS = [
    ("projects", "credential", True),
    ("execution_environments", "credential", True),
    ("inventory_sources", "credential", True),
    ("credential_input_sources", "source_credential", True),
    ("instance_groups", "credential", False),
]


async def _build_credential_review(
    src_client: Any,
    created_creds: list[dict[str, str]],
    org_ids: list[int],
) -> list[dict[str, Any]]:
    """Query source AAP to find which created credentials are actually used."""
    import asyncio

    cred_source_ids = {c["source_id"] for c in created_creds}
    used_by: dict[str, list[dict[str, str]]] = {sid: [] for sid in cred_source_ids}

    async def _query_consumer(resource_type: str, field_name: str, filter_org: bool) -> None:
        try:
            resp = await src_client.get(f"{resource_type}/", params={"page_size": 200})
            for item in resp.get("results", []):
                cred_ref = item.get(field_name)
                if cred_ref is None or str(cred_ref) not in cred_source_ids:
                    continue
                if filter_org and org_ids:
                    item_org = item.get("organization") or (
                        item.get("summary_fields", {}).get("organization", {}).get("id")
                    )
                    if item_org and item_org not in org_ids:
                        continue
                used_by[str(cred_ref)].append(
                    {
                        "resource_type": resource_type,
                        "resource_name": item.get("name", str(item.get("id", "?"))),
                    }
                )
        except Exception:  # nosec B110
            pass

    async def _query_galaxy(org_id: int) -> None:
        try:
            resp = await src_client.get(f"organizations/{org_id}/galaxy_credentials/")
            for gc in resp.get("results", []):
                gc_id = str(gc.get("id", ""))
                if gc_id in cred_source_ids:
                    used_by[gc_id].append(
                        {
                            "resource_type": "organizations (galaxy)",
                            "resource_name": f"Org {org_id}",
                        }
                    )
        except Exception:  # nosec B110
            pass

    await asyncio.gather(
        *[_query_consumer(rt, fn, fo) for rt, fn, fo in _CRED_CONSUMERS],
        *[_query_galaxy(oid) for oid in org_ids],
    )

    result: list[dict[str, Any]] = []
    for cred in created_creds:
        sid = cred["source_id"]
        result.append(
            {
                "name": cred["name"],
                "credential_type": cred["credential_type"],
                "organization": cred["organization"],
                "used_by": used_by.get(sid, []),
            }
        )
    result.sort(key=lambda c: (len(c["used_by"]) == 0, c["credential_type"], c["name"]))
    return result


# ---------------------------------------------------------------------------
# Phase execution helpers
# ---------------------------------------------------------------------------


def _build_source_contexts(
    source_configs: list[dict[str, Any]],
    dest_cfg: Any,
    dest_auth_scheme: str,
    db_url: str,
) -> tuple[Any, Any, list[dict[str, Any]]]:
    """Build the target client and per-source context dicts."""
    from aap_migration.client.aap_source_client import AAPSourceClient
    from aap_migration.client.aap_target_client import AAPTargetClient
    from aap_migration.config import AAPInstanceConfig, MigrationConfig, StateConfig
    from aap_migration.migration.state import MigrationState

    target_config = AAPInstanceConfig(
        url=dest_cfg.url,
        token=dest_cfg.token,
        verify_ssl=dest_cfg.verify_ssl,
        timeout=dest_cfg.timeout,
    )
    target_client = AAPTargetClient(target_config, auth_scheme=dest_auth_scheme)

    sources: list[dict[str, Any]] = []
    for src_cfg in source_configs:
        src_config = AAPInstanceConfig(
            url=src_cfg["url"],
            token=src_cfg["token"],
            verify_ssl=src_cfg["verify_ssl"],
            timeout=src_cfg["timeout"],
        )
        migration_config = MigrationConfig(
            source=src_config,
            target=target_config,
            state=StateConfig(db_path=db_url),
        )
        sources.append(
            {
                "src_config": src_config,
                "migration_config": migration_config,
                "src_client": AAPSourceClient(
                    src_config,
                    auth_scheme=src_cfg.get("auth_scheme", "Bearer"),
                ),
                "state": MigrationState(migration_config.state),
                "name_prefix": src_cfg.get("name_prefix", ""),
                "org_ids": src_cfg["org_ids"],
                "url": src_cfg["url"],
            }
        )
    return target_config, target_client, sources


async def _migrate_resource_type(
    rtype: str,
    sources: list[dict[str, Any]],
    target_client: Any,
    phase_num: int,
    emit: Callable[[dict[str, Any]], None],
    log: Callable[[str], None],
    created_creds: list[dict[str, str]],
) -> tuple[int, int, int, int]:
    """Export → filter → transform → import one resource type across all sources.

    Returns (created, skipped, failed, exported).
    """
    import time

    from aap_migration.migration.exporter import create_exporter
    from aap_migration.migration.importer import create_importer
    from aap_migration.migration.transformer import SkipResourceError, create_transformer
    from aap_migration.resources import RESOURCE_REGISTRY

    info = RESOURCE_REGISTRY[rtype]
    phase_start = time.monotonic()
    created = 0
    skipped = 0
    failed = 0
    exported = 0
    last_progress = time.monotonic()
    PROGRESS_INTERVAL = 2.0

    for src in sources:
        src_client = src["src_client"]
        state = src["state"]
        migration_config = src["migration_config"]
        name_prefix: str = src["name_prefix"]
        org_ids: list[int] = src["org_ids"]

        try:
            exporter = create_exporter(
                resource_type=rtype,
                client=src_client,
                state=state,
                performance_config=migration_config.performance,
            )
            transformer = (
                create_transformer(
                    resource_type=rtype, dry_run=False, state=state, defer_project_sync=False
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

            async for resource in exporter.export():
                source_id = resource.get("id")
                if source_id is None:
                    if rtype == "host_inventory_memberships":
                        source_id = f"{resource.get('host_id')}_{resource.get('inventory_id')}"
                    elif rtype == "settings":
                        source_id = "settings"
                    else:
                        continue

                if org_ids and not _resource_in_orgs(rtype, resource, source_id, org_ids):
                    continue

                raw_summary = resource.get("summary_fields", {})

                if transformer:
                    try:
                        resource = transformer.transform_resource(
                            resource_type=rtype, data=resource, validate=True
                        )
                    except SkipResourceError:
                        skipped += 1
                        continue
                    except Exception:
                        failed += 1
                        continue

                if name_prefix and rtype not in ("users", "settings", "host_inventory_memberships"):
                    if "name" in resource:
                        resource["name"] = f"{name_prefix}{resource['name']}"

                exported += 1

                try:
                    if rtype == "host_inventory_memberships":
                        result = await cast(Any, importer).import_resource(resource=resource)
                    else:
                        result = await importer.import_resource(
                            resource_type=rtype,
                            source_id=int(source_id),
                            data=resource,
                        )
                    res_name = resource.get("name", resource.get("username", str(source_id)))
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
                        if rtype == "credentials":
                            created_creds.append(
                                {
                                    "name": res_name,
                                    "credential_type": raw_summary.get("credential_type", {}).get(
                                        "name", "Unknown"
                                    ),
                                    "organization": raw_summary.get("organization", {}).get(
                                        "name", ""
                                    ),
                                    "source_id": str(source_id),
                                }
                            )
                    else:
                        skipped += 1
                except Exception as exc:
                    failed += 1
                    emit(
                        {
                            "_event": "resource_result",
                            "phase_num": phase_num,
                            "name": resource.get("name", resource.get("username", str(source_id))),
                            "resource_type": rtype,
                            "result": "failed",
                            "detail": str(exc)[:200],
                        }
                    )

                now = time.monotonic()
                if now - last_progress >= PROGRESS_INTERVAL:
                    emit(
                        {
                            "_event": "phase_progress",
                            "phase_num": phase_num,
                            "exported": exported,
                            "created": created,
                            "skipped": skipped,
                            "failed": failed,
                            "rate": f"{exported / max(now - phase_start, 0.1):.0f}/s",
                            "elapsed": f"{now - phase_start:.1f}s",
                        }
                    )
                    last_progress = now

        except Exception as exc:
            failed += 1
            emit({"_event": "phase_error", "phase_num": phase_num, "error": str(exc)})
            log(f"  Error on {rtype} from {src['url']}: {exc}")

    duration = f"{time.monotonic() - phase_start:.1f}s"
    emit(
        {
            "_event": "phase_complete",
            "phase_num": phase_num,
            "description": info.description,
            "created": created,
            "updated": 0,
            "skipped": skipped,
            "failed": failed,
            "exported": exported,
            "duration": duration,
            "warnings": {},
        }
    )

    return created, skipped, failed, exported


def _resource_in_orgs(
    rtype: str, resource: dict[str, Any], source_id: Any, org_ids: list[int]
) -> bool:
    """Check whether a resource belongs to one of the selected orgs."""
    if rtype == "organizations":
        return source_id in org_ids
    if rtype in ("settings", "host_inventory_memberships"):
        return True
    res_org = resource.get("organization")
    sf_org = resource.get("summary_fields", {}).get("organization", {}).get("id")
    if res_org in org_ids or sf_org in org_ids:
        return True
    return res_org is None and sf_org is None


async def _handle_credential_pause(
    job: Job,
    svc: Any,
    created_creds: list[dict[str, str]],
    sources: list[dict[str, Any]],
    plan_id: str,
    phase_id: str,
    emit: Callable[[dict[str, Any]], None],
    log: Callable[[str], None],
) -> None:
    """Pause migration for credential secret review, wait for user to resume."""
    import asyncio

    all_org_ids: list[int] = []
    all_src_clients = []
    for s in sources:
        all_org_ids.extend(s["org_ids"])
        all_src_clients.append(s["src_client"])

    reviews = await asyncio.gather(
        *[_build_credential_review(sc, created_creds, all_org_ids) for sc in all_src_clients]
    )
    cred_review: list[dict[str, Any]] = []
    for r in reviews:
        cred_review.extend(r)

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for cr in cred_review:
        if cr["name"] not in seen:
            seen.add(cr["name"])
            deduped.append(cr)
    cred_review = deduped

    if not cred_review:
        return

    emit({"_event": "credential_pause", "credentials": cred_review})
    log("Paused — waiting for user to update credential secrets on the target and resume.")
    job.result = job.result or {}
    job.result["credential_review"] = cred_review
    job.result["_paused_plan_id"] = plan_id
    job.result["_paused_phase_id"] = phase_id
    job.status = JobStatus.WAITING_FOR_INPUT
    svc.persist_job(job)
    await job.wait_for_resume()
    job.status = JobStatus.RUNNING
    log("Resumed — continuing migration.")


async def _run_cac_org_update(
    sources: list[dict[str, Any]],
    target_client: Any,
    phase_num: int,
    emit: Callable[[dict[str, Any]], None],
    log: Callable[[str], None],
) -> int:
    """CaC org-update pass: PATCH orgs to assign EE, galaxy creds, etc."""
    import asyncio

    sem = asyncio.Semaphore(5)

    async def _update_one_org(src: dict[str, Any], org_id: int) -> int:
        async with sem:
            try:
                src_client = src["src_client"]
                state = src["state"]
                org_data = await src_client.get(f"organizations/{org_id}/")
                target_org_id = state.get_mapped_id("organizations", org_id)
                if target_org_id is None:
                    return 0
                patch: dict[str, Any] = {}
                if org_data.get("default_environment"):
                    mapped_ee = state.get_mapped_id(
                        "execution_environments", org_data["default_environment"]
                    )
                    if mapped_ee:
                        patch["default_environment"] = mapped_ee
                count = 0
                if patch:
                    await target_client.update_resource("organizations", target_org_id, patch)
                    count += 1
                    emit(
                        {
                            "_event": "resource_result",
                            "phase_num": phase_num,
                            "name": org_data.get("name", str(org_id)),
                            "resource_type": "organizations",
                            "result": "updated",
                            "detail": "CaC org-update pass",
                        }
                    )

                try:
                    galaxy_resp = await src_client.get(
                        f"organizations/{org_id}/galaxy_credentials/"
                    )
                    for gc in galaxy_resp.get("results", []):
                        gc_source_id = gc.get("id")
                        if gc_source_id is None:
                            continue
                        mapped_gc = state.get_mapped_id("credentials", gc_source_id)
                        if mapped_gc:
                            await target_client.post(
                                f"organizations/{target_org_id}/galaxy_credentials/",
                                {"id": mapped_gc},
                            )
                except Exception as gc_exc:
                    log(f"  Warning: galaxy cred association for org {org_id}: {gc_exc}")
                return count
            except Exception as org_exc:
                log(f"  Warning: CaC org-update for {org_id}: {org_exc}")
                return 0

    tasks = [_update_one_org(src, oid) for src in sources for oid in src["org_ids"]]
    results = await asyncio.gather(*tasks)
    return sum(results)


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
                "org_ids": org_ids,
                "auth_scheme": ConnectionService._auth_scheme(conn),
            }
        )

    dest_cfg = ConnectionService.build_instance_config(dest)
    dest_auth_scheme = ConnectionService._auth_scheme(dest)

    phase_name = phase.name or f"Phase {phase.phase_number}"
    db_url = get_db_url()

    phase.status = PhaseStatus.RUNNING
    db.flush()

    svc = get_job_service()
    session_factory = get_app_state().db_session_factory

    async def _do_phase(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        from aap_migration.resources import RESOURCE_REGISTRY, get_fully_supported_types

        def emit(event: dict[str, Any]) -> None:
            log("\t" + json.dumps(event))

        totals = {"created": 0, "skipped": 0, "failed": 0, "updated": 0}

        resource_order = get_fully_supported_types()
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

    job_id = svc.start_job(f"Plan: {phase_name}", "migration-run", _do_phase)

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
