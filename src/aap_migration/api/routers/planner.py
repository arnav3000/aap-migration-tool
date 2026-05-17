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


def _get_importer_deps() -> dict[str, list[str]]:
    """Build resource_type -> list of dependency resource types from importer classes."""
    from aap_migration.migration.importer import (
        ApplicationImporter,
        CredentialImporter,
        CredentialInputSourceImporter,
        CredentialTypeImporter,
        ExecutionEnvironmentImporter,
        HostImporter,
        HostInventoryMembershipImporter,
        InstanceGroupImporter,
        InstanceImporter,
        InventoryGroupImporter,
        InventoryImporter,
        InventorySourceImporter,
        JobTemplateImporter,
        LabelImporter,
        NotificationTemplateImporter,
        OrganizationImporter,
        ProjectImporter,
        ScheduleImporter,
        SettingsImporter,
        SystemJobTemplateImporter,
        TeamImporter,
        UserImporter,
        WorkflowImporter,
    )

    _importer_map: dict[str, type] = {
        "organizations": OrganizationImporter,
        "labels": LabelImporter,
        "instances": InstanceImporter,
        "instance_groups": InstanceGroupImporter,
        "users": UserImporter,
        "teams": TeamImporter,
        "credential_types": CredentialTypeImporter,
        "credentials": CredentialImporter,
        "credential_input_sources": CredentialInputSourceImporter,
        "projects": ProjectImporter,
        "execution_environments": ExecutionEnvironmentImporter,
        "inventories": InventoryImporter,
        "inventory_sources": InventorySourceImporter,
        "inventory_groups": InventoryGroupImporter,
        "hosts": HostImporter,
        "host_inventory_memberships": HostInventoryMembershipImporter,
        "job_templates": JobTemplateImporter,
        "workflow_job_templates": WorkflowImporter,
        "schedules": ScheduleImporter,
        "notification_templates": NotificationTemplateImporter,
        "applications": ApplicationImporter,
        "settings": SettingsImporter,
        "system_job_templates": SystemJobTemplateImporter,
    }

    result: dict[str, list[str]] = {}
    for rtype, cls in _importer_map.items():
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
            status="pending",
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


_CRITICAL_CRED_CONSUMERS = [
    ("projects", "credential"),
    ("execution_environments", "credential"),
    ("inventory_sources", "credential"),
    ("credential_input_sources", "source_credential"),
]

_CONTAINER_GROUP_CONSUMERS = [
    ("instance_groups", "credential"),
]


async def _build_credential_review(
    src_client: Any,
    created_creds: list[dict[str, str]],
    org_ids: list[int],
) -> list[dict[str, Any]]:
    """Query source AAP to find which created credentials are actually used."""
    cred_source_ids = {c["source_id"] for c in created_creds}
    used_by: dict[str, list[dict[str, str]]] = {sid: [] for sid in cred_source_ids}

    for resource_type, field_name in _CRITICAL_CRED_CONSUMERS:
        try:
            resp = await src_client.get(f"{resource_type}/", params={"page_size": 200})
            for item in resp.get("results", []):
                cred_ref = item.get(field_name)
                if cred_ref is not None and str(cred_ref) in cred_source_ids:
                    item_org = item.get("organization") or (
                        item.get("summary_fields", {}).get("organization", {}).get("id")
                    )
                    if org_ids and item_org and item_org not in org_ids:
                        continue
                    used_by[str(cred_ref)].append(
                        {
                            "resource_type": resource_type,
                            "resource_name": item.get("name", str(item.get("id", "?"))),
                        }
                    )
        except Exception:  # nosec B110
            pass

    for resource_type, field_name in _CONTAINER_GROUP_CONSUMERS:
        try:
            resp = await src_client.get(f"{resource_type}/", params={"page_size": 200})
            for item in resp.get("results", []):
                cred_ref = item.get(field_name)
                if cred_ref is not None and str(cred_ref) in cred_source_ids:
                    used_by[str(cred_ref)].append(
                        {
                            "resource_type": "instance_groups",
                            "resource_name": item.get("name", str(item.get("id", "?"))),
                        }
                    )
        except Exception:  # nosec B110
            pass

    for org_id in org_ids:
        try:
            resp = await src_client.get(
                f"organizations/{org_id}/galaxy_credentials/",
            )
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

    result: list[dict[str, Any]] = []
    for cred in created_creds:
        sid = cred["source_id"]
        usages = used_by.get(sid, [])
        result.append(
            {
                "name": cred["name"],
                "credential_type": cred["credential_type"],
                "organization": cred["organization"],
                "used_by": usages,
            }
        )

    result.sort(key=lambda c: (len(c["used_by"]) == 0, c["credential_type"], c["name"]))
    return result


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
            if RESOURCE_REGISTRY[rt].has_exporter and RESOURCE_REGISTRY[rt].has_importer
        ]
        num_resource_types = len(resource_order)

        CRED_PAUSE_AFTER = {"credentials", "credential_input_sources"}
        created_creds: list[dict[str, str]] = []

        # Pre-build per-source objects
        _sources: list[dict[str, Any]] = []
        try:
            target_config = AAPInstanceConfig(
                url=dest_cfg.url,
                token=dest_cfg.token,
                verify_ssl=dest_cfg.verify_ssl,
                timeout=dest_cfg.timeout,
            )
            target_client = AAPTargetClient(target_config, auth_scheme=dest_auth_scheme)

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
                _sources.append(
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

            # Outer loop: resource types; inner loop: sources
            phase_num = 0
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
                exported = 0
                last_progress = time.monotonic()
                PROGRESS_INTERVAL = 2.0

                for src in _sources:
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

                            raw_summary = resource.get("summary_fields", {})

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

                            if name_prefix and rtype not in (
                                "users",
                                "settings",
                                "host_inventory_memberships",
                            ):
                                if "name" in resource:
                                    resource["name"] = f"{name_prefix}{resource['name']}"

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
                                    if rtype == "credentials":
                                        cred_type_name = raw_summary.get("credential_type", {}).get(
                                            "name", "Unknown"
                                        )
                                        cred_org_name = raw_summary.get("organization", {}).get(
                                            "name", ""
                                        )
                                        created_creds.append(
                                            {
                                                "name": res_name,
                                                "credential_type": cred_type_name,
                                                "organization": cred_org_name,
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

                total_created += created
                total_skipped += skipped
                total_failed += failed

                # --- Credential pause: after ALL sources' creds finish ---
                if rtype in CRED_PAUSE_AFTER:
                    remaining = CRED_PAUSE_AFTER - set(
                        resource_order[: resource_order.index(rtype) + 1]
                    )
                    if not remaining and created_creds:
                        all_org_ids: list[int] = []
                        all_src_clients = []
                        for s in _sources:
                            all_org_ids.extend(s["org_ids"])
                            all_src_clients.append(s["src_client"])

                        cred_review: list[dict[str, Any]] = []
                        for sc in all_src_clients:
                            cred_review.extend(
                                await _build_credential_review(
                                    sc,
                                    created_creds,
                                    all_org_ids,
                                )
                            )
                        # Deduplicate by credential name
                        seen: set[str] = set()
                        deduped: list[dict[str, Any]] = []
                        for cr in cred_review:
                            if cr["name"] not in seen:
                                seen.add(cr["name"])
                                deduped.append(cr)
                        cred_review = deduped

                        if cred_review:
                            emit(
                                {
                                    "_event": "credential_pause",
                                    "credentials": cred_review,
                                }
                            )
                            log(
                                "Paused — waiting for user to update credential "
                                "secrets on the target and resume."
                            )
                            job.result = job.result or {}
                            job.result["credential_review"] = cred_review
                            job.result["_paused_plan_id"] = plan_id
                            job.result["_paused_phase_id"] = phase_id
                            job.status = "waiting_for_input"
                            svc._persist_job(job)
                            await job._resume_event.wait()
                            job._resume_event.clear()
                            job.status = "running"
                            log("Resumed — continuing migration.")

            # --- CaC org-update pass: PATCH orgs to assign EE, galaxy creds, etc. ---
            log("CaC pass: re-patching organizations with final references...")
            for src in _sources:
                src_client = src["src_client"]
                state = src["state"]
                org_ids = src["org_ids"]
                for org_id in org_ids:
                    try:
                        org_data = await src_client.get(f"organizations/{org_id}/")
                        target_org_id = state.get_mapped_id("organizations", org_id)
                        if target_org_id is None:
                            continue
                        patch: dict[str, Any] = {}
                        if org_data.get("default_environment"):
                            mapped_ee = state.get_mapped_id(
                                "execution_environments",
                                org_data["default_environment"],
                            )
                            if mapped_ee:
                                patch["default_environment"] = mapped_ee
                        if patch:
                            await target_client.update_resource(
                                "organizations",
                                target_org_id,
                                patch,
                            )
                            total_updated += 1
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
                                f"organizations/{org_id}/galaxy_credentials/",
                            )
                            galaxy_creds = galaxy_resp.get("results", [])
                            for gc in galaxy_creds:
                                gc_source_id = gc.get("id")
                                if gc_source_id is None:
                                    continue
                                mapped_gc = state.get_mapped_id(
                                    "credentials",
                                    gc_source_id,
                                )
                                if mapped_gc:
                                    await target_client.post(
                                        f"organizations/{target_org_id}/galaxy_credentials/",
                                        {"id": mapped_gc},
                                    )
                        except Exception as gc_exc:
                            log(f"  Warning: galaxy cred association for org {org_id}: {gc_exc}")
                    except Exception as org_exc:
                        log(f"  Warning: CaC org-update for {org_id}: {org_exc}")

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
