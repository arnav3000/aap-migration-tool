from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from aap_migration.api import models as api_models
from aap_migration.api.routers import planner
from aap_migration.api.schemas import (
    PhaseOrgUpdate,
    PhasesUpdateRequest,
    PhaseUpdate,
    PlanCreate,
    PlanSourceCreate,
    PlanSourceUpdate,
    PlanUpdate,
)


def _make_connection(db_session, conn_id: str, name: str) -> api_models.Connection:
    conn = api_models.Connection(
        id=conn_id,
        name=name,
        url=f"https://{name.lower()}.example.com",
        token="token",
        role="source",
        ping_status="ok",
        auth_status="ok",
    )
    db_session.add(conn)
    db_session.flush()
    return conn


def test_planner_crud_update_and_populate(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = _make_connection(db_session, "dest-1", "Dest")
    source = _make_connection(db_session, "src-1", "Source")
    analysis_job = api_models.JobRecord(
        id="job-1",
        seq_id=1,
        name="analysis",
        type="analysis",
        status="completed",
    )
    db_session.add(analysis_job)
    db_session.flush()

    monkeypatch.setattr(
        planner,
        "_get_importer_deps",
        lambda: {"organizations": [], "users": ["organizations"]},
    )

    resource_types = planner.list_resource_types()
    assert any(item["name"] == "organizations" for item in resource_types)
    assert any(
        item["name"] == "users" and item["dependencies"] == ["organizations"]
        for item in resource_types
    )

    plan = planner.create_plan(
        PlanCreate(
            name="Wave plan",
            description="demo",
            destination_id=dest.id,
            sources=[
                PlanSourceCreate(
                    connection_id=source.id,
                    name_prefix="pre-",
                    analysis_job_id=analysis_job.id,
                )
            ],
        ),
        db=db_session,
    )
    assert plan["name"] == "Wave plan"
    plan_id = plan["id"]
    source_row_id = plan["sources"][0]["id"]

    listed = planner.list_plans(db=db_session)
    assert listed[0]["source_count"] == 1
    assert listed[0]["phase_count"] == 0

    fetched = planner.get_plan(plan_id, db=db_session)
    assert fetched["destination_id"] == dest.id

    updated = planner.update_plan(
        plan_id,
        PlanUpdate(name="Updated", description="changed", status="active"),
        db=db_session,
    )
    assert updated["name"] == "Updated"
    assert updated["status"] == "active"

    phase_payload = PhasesUpdateRequest(
        sources=[
            PlanSourceUpdate(
                id=source_row_id,
                connection_id=source.id,
                name_prefix="phase-",
                analysis_job_id=analysis_job.id,
            )
        ],
        phases=[
            PhaseUpdate(
                phase_number=1,
                name="Phase One",
                resource_types=["organizations", "users"],
                orgs=[PhaseOrgUpdate(source_id=source_row_id, org_id=7, org_name="Org 7")],
            )
        ],
    )
    phased = planner.update_phases(plan_id, phase_payload, db=db_session)
    assert phased["phases"][0]["resource_types"] == ["organizations", "users"]
    assert phased["phases"][0]["orgs"][0]["org_name"] == "Org 7"

    class FakeJobService:
        def get_job(self, job_id):
            return SimpleNamespace(
                result={
                    "organizations": {
                        "Org A": {"org_id": 11},
                        "Org B": {"org_id": 12},
                    },
                    "migration_phases": [
                        {"phase": 1, "orgs": ["Org A"]},
                        {"phase": 2, "orgs": {"orgs": ["Org B"]}},
                    ],
                }
            )

    monkeypatch.setattr(planner, "get_job_service", lambda: FakeJobService())
    populated = planner.populate_plan(plan_id, db=db_session)
    assert [phase["name"] for phase in populated["phases"]] == [
        "Wave 1 (1 orgs)",
        "Wave 2 (1 orgs)",
    ]
    assert populated["phases"][1]["orgs"][0]["org_name"] == "Org B"

    planner.delete_plan(plan_id, db=db_session)
    db_session.flush()
    assert db_session.get(api_models.MigrationPlan, plan_id) is None


@pytest.mark.asyncio
async def test_planner_credential_review_and_execute_phase(
    db_session,
    session_factory,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_client = SimpleNamespace()

    async def review_get(endpoint, params=None):
        if endpoint == "projects/":
            return {"results": [{"id": 1, "name": "Proj", "credential": 100, "organization": 5}]}
        if endpoint == "instance_groups/":
            return {"results": [{"id": 2, "name": "IG", "credential": 100}]}
        if endpoint == "organizations/5/galaxy_credentials/":
            return {"results": [{"id": 100}]}
        return {"results": []}

    review_client.get = review_get
    review = await planner._build_credential_review(
        review_client,
        [
            {
                "name": "Machine",
                "credential_type": "ssh",
                "organization": "Default",
                "source_id": "100",
            }
        ],
        [5],
    )
    assert review[0]["used_by"][0]["resource_type"] == "projects"
    assert any(item["resource_type"] == "instance_groups" for item in review[0]["used_by"])
    assert any(item["resource_type"] == "organizations (galaxy)" for item in review[0]["used_by"])

    dest = _make_connection(db_session, "dest-2", "Dest2")
    source = _make_connection(db_session, "src-2", "Source2")
    plan = api_models.MigrationPlan(
        id="plan-1", name="Plan", description="", destination_id=dest.id, status="draft"
    )
    db_session.add(plan)
    plan_source = api_models.MigrationPlanSource(
        id="plan-source-1",
        plan_id=plan.id,
        connection_id=source.id,
        name_prefix="pref-",
    )
    phase = api_models.MigrationPlanPhase(
        id="phase-1",
        plan_id=plan.id,
        phase_number=1,
        name="Phase 1",
        status="pending",
    )
    db_session.add_all([plan_source, phase])
    db_session.flush()
    phase_org = api_models.MigrationPlanPhaseOrg(
        id="phase-org-1",
        phase_id=phase.id,
        source_id=plan_source.id,
        org_id=1,
        org_name="Default",
    )
    db_session.add(phase_org)
    db_session.flush()
    db_session.add(
        api_models.JobRecord(
            id="migration-run-job",
            seq_id=2,
            name="phase job",
            type="migration-run",
            status="pending",
        )
    )
    db_session.flush()

    monkeypatch.setattr(
        planner.ConnectionService,
        "get",
        lambda db, conn_id: {"dest-2": dest, "src-2": source}.get(conn_id),
    )
    monkeypatch.setattr(
        planner.ConnectionService,
        "build_instance_config",
        lambda conn: SimpleNamespace(url=conn.url, token="secret", verify_ssl=True, timeout=30),
    )
    monkeypatch.setattr(planner.ConnectionService, "_auth_scheme", lambda conn: "Token")
    monkeypatch.setattr(planner, "get_db_url", lambda: str(tmp_path / "planner.db"))
    monkeypatch.setattr(
        planner, "get_app_state", lambda: SimpleNamespace(db_session_factory=session_factory)
    )
    status_updates = []
    monkeypatch.setattr(
        planner,
        "_update_phase_status",
        lambda sf, phase_id, status: status_updates.append((phase_id, status)),
    )
    monkeypatch.setattr("aap_migration.resources.get_migration_order", lambda: ["organizations"])

    class FakeJobService:
        def __init__(self) -> None:
            self.started = []

        def start_job(self, name, job_type, callback):
            self.started.append((name, job_type, callback))
            return "migration-run-job"

        def _persist_job(self, job):
            return None

    svc = FakeJobService()
    monkeypatch.setattr(planner, "get_job_service", lambda: svc)

    monkeypatch.setattr(
        "aap_migration.config.AAPInstanceConfig", lambda **kwargs: SimpleNamespace(**kwargs)
    )
    monkeypatch.setattr(
        "aap_migration.config.StateConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        "aap_migration.config.MigrationConfig",
        lambda **kwargs: SimpleNamespace(
            source=kwargs["source"],
            target=kwargs["target"],
            state=kwargs["state"],
            performance=SimpleNamespace(),
            resource_mappings={},
        ),
    )

    class FakeSourceClient:
        def __init__(self, config, auth_scheme=None):
            self.config = config

        async def get(self, endpoint, params=None):
            if endpoint == "organizations/1/":
                return {"id": 1, "name": "Default", "default_environment": None}
            if endpoint == "organizations/1/galaxy_credentials/":
                return {"results": []}
            return {"results": []}

    class FakeTargetClient:
        def __init__(self, config, auth_scheme=None):
            self.config = config

        async def update_resource(self, resource_type, resource_id, patch):
            return None

        async def post(self, endpoint, payload):
            return None

    class FakeState:
        def __init__(self, config):
            self.config = config

        def get_mapped_id(self, resource_type, source_id):
            if resource_type == "organizations" and source_id == 1:
                return 101
            return None

    class FakeExporter:
        async def export(self):
            yield {"id": 1, "name": "Default"}

    class FakeImporter:
        async def import_resource(self, resource_type, source_id, data):
            return True

    monkeypatch.setattr("aap_migration.client.aap_source_client.AAPSourceClient", FakeSourceClient)
    monkeypatch.setattr("aap_migration.client.aap_target_client.AAPTargetClient", FakeTargetClient)
    monkeypatch.setattr("aap_migration.migration.state.MigrationState", FakeState)
    monkeypatch.setattr(
        "aap_migration.migration.exporter.create_exporter", lambda **kwargs: FakeExporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.importer.create_importer", lambda **kwargs: FakeImporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.transformer.create_transformer", lambda **kwargs: None
    )

    response = await planner.execute_phase(plan.id, phase.id, db=db_session)
    assert response.job_id == "migration-run-job"
    assert phase.status == "running"
    assert phase.job_id == "migration-run-job"
    _, _, callback = svc.started[0]

    job = SimpleNamespace(result=None, status="running", _resume_event=asyncio.Event())
    logs = []
    result = await callback(job, logs.append)
    assert result == {
        "total_created": 1,
        "total_updated": 0,
        "total_skipped": 0,
        "total_failed": 0,
    }
    events = [json.loads(line[1:]) for line in logs if line.startswith("\t{")]
    assert any(event["_event"] == "phase_start" for event in events)
    assert any(
        event["_event"] == "resource_result" and event["result"] == "created" for event in events
    )
    assert any(event["_event"] == "migration_complete" for event in events)
    assert status_updates == [("phase-1", "completed")]


def test_planner_update_phase_status(session_factory, db_session) -> None:
    phase = api_models.MigrationPlanPhase(
        id="phase-status",
        plan_id="plan-status",
        phase_number=1,
        name="Status",
        status="pending",
    )
    plan = api_models.MigrationPlan(id="plan-status", name="Plan", description="", status="draft")
    db_session.add(plan)
    db_session.add(phase)
    db_session.commit()

    planner._update_phase_status(session_factory, phase.id, "completed")

    refreshed = db_session.get(api_models.MigrationPlanPhase, phase.id)
    db_session.refresh(refreshed)
    assert refreshed.status == "completed"
