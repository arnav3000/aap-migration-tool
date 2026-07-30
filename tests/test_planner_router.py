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
                "source": "Prod AAP",
                "name_prefix": "prod_",
            }
        ],
        [5],
    )
    assert review[0]["used_by"][0]["resource_type"] == "projects"
    assert review[0]["source"] == "Prod AAP"
    assert review[0]["name_prefix"] == "prod_"
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

        def persist_job(self, job):
            return None

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
        def __init__(self, config, source_key: str = "", **_kwargs):
            self.config = config
            self.source_key = source_key

        def get_mapped_id(self, resource_type, source_id):
            if resource_type == "organizations" and source_id == 1:
                return 101
            return None

    class FakeExporter:
        async def export(self):
            yield {"id": 1, "name": "Default", "organization": 1}

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

    async def wait_for_resume() -> None:
        await job._resume_event.wait()
        job._resume_event.clear()

    job.wait_for_resume = wait_for_resume
    job._resume_event.set()
    logs = []
    result = await callback(job, logs.append)
    assert result["created"] >= 1
    assert result["updated"] == 0
    assert result["skipped"] == 0
    assert "failed" in result
    events = [json.loads(line[1:]) for line in logs if line.startswith("\t{")]
    assert any(event["_event"] == "phase_start" for event in events)
    assert any(
        event["_event"] == "resource_result" and event["result"] == "created" for event in events
    )
    assert any(event["_event"] == "migration_complete" for event in events)
    assert status_updates[0][0] == "phase-1"
    assert status_updates[0][1] in {"completed", "completed_with_errors"}


@pytest.mark.asyncio
async def test_execute_phase_persists_job_then_links_phase(
    db_session,
    session_factory,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: starting the job must not race a request-session SQLite write lock.

    execute_phase used to flush phase.status=running before start_job. That held the
    SQLite write lock, so _persist_job_initial failed silently, then setting
    phase.job_id raised FOREIGN KEY constraint failed — while the in-memory job
    kept running (visible under Jobs, invisible to the planner).
    """
    from aap_migration.api.services.job_service import JobService

    dest = _make_connection(db_session, "dest-fk", "DestFk")
    source = _make_connection(db_session, "src-fk", "SourceFk")
    plan = api_models.MigrationPlan(
        id="plan-fk", name="Plan FK", description="", destination_id=dest.id, status="draft"
    )
    db_session.add(plan)
    plan_source = api_models.MigrationPlanSource(
        id="plan-source-fk",
        plan_id=plan.id,
        connection_id=source.id,
    )
    phase = api_models.MigrationPlanPhase(
        id="phase-fk",
        plan_id=plan.id,
        phase_number=1,
        name="Phase FK",
        status="pending",
    )
    db_session.add_all([plan_source, phase])
    db_session.flush()
    db_session.add(
        api_models.MigrationPlanPhaseOrg(
            id="phase-org-fk",
            phase_id=phase.id,
            source_id=plan_source.id,
            org_id=1,
            org_name="Default",
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        planner.ConnectionService,
        "get",
        lambda db, conn_id: {"dest-fk": dest, "src-fk": source}.get(conn_id),
    )
    monkeypatch.setattr(
        planner.ConnectionService,
        "build_instance_config",
        lambda conn: SimpleNamespace(url=conn.url, token="secret", verify_ssl=True, timeout=30),
    )
    monkeypatch.setattr(planner.ConnectionService, "_auth_scheme", lambda conn: "Token")
    monkeypatch.setattr(planner, "get_db_url", lambda: str(tmp_path / "planner-fk.db"))
    monkeypatch.setattr(
        planner, "get_app_state", lambda: SimpleNamespace(db_session_factory=session_factory)
    )

    svc = JobService(db_session_factory=session_factory)
    monkeypatch.setattr(planner, "get_job_service", lambda: svc)

    # Re-open as an uncommitted request session that already loaded the phase
    # (mirrors FastAPI get_db usage during execute_phase).
    request_db = session_factory()
    response = None
    job_id: str | None = None
    try:
        response = await planner.execute_phase(plan.id, phase.id, db=request_db)
        job_id = response.job_id
        request_db.commit()
    finally:
        if job_id:
            job = svc.get_job(job_id)
            if job is not None and job._task is not None:
                job._task.cancel()
                try:
                    await job._task
                except (asyncio.CancelledError, Exception):
                    pass
        request_db.close()

    assert response is not None
    assert response.job_id
    linked = session_factory()
    try:
        refreshed = linked.get(api_models.MigrationPlanPhase, phase.id)
        assert refreshed is not None
        assert refreshed.status == "running"
        assert refreshed.job_id == response.job_id
        job_row = linked.get(api_models.JobRecord, response.job_id)
        assert job_row is not None
        assert job_row.type == "migration-run"
        plan_row = linked.get(api_models.MigrationPlan, plan.id)
        assert plan_row is not None
        assert plan_row.status == "active"
    finally:
        linked.close()


@pytest.mark.asyncio
async def test_credential_pause_reviews_each_source_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-source pause must not query source B with source A's credential IDs."""
    calls: list[tuple[str, list[str]]] = []

    async def fake_review(src_client, created_creds, org_ids):
        label = getattr(src_client, "label", "?")
        calls.append((label, [c["source_id"] for c in created_creds]))
        return [
            {
                "name": c["name"],
                "credential_type": c["credential_type"],
                "organization": c["organization"],
                "source": c.get("source", ""),
                "name_prefix": c.get("name_prefix", ""),
                "used_by": [{"resource_type": "projects", "resource_name": f"from-{label}"}],
            }
            for c in created_creds
        ]

    monkeypatch.setattr(planner, "_build_credential_review", fake_review)

    created_creds = [
        {
            "name": "dev_Shared",
            "credential_type": "ssh",
            "organization": "Default",
            "source_id": "10",
            "source": "Dev AAP",
            "name_prefix": "dev_",
        },
        {
            "name": "prod_Shared",
            "credential_type": "ssh",
            "organization": "Default",
            "source_id": "10",
            "source": "Prod AAP",
            "name_prefix": "prod_",
        },
    ]
    sources = [
        {
            "connection_name": "Dev AAP",
            "url": "https://dev.example.com",
            "name_prefix": "dev_",
            "org_ids": [1],
            "src_client": SimpleNamespace(label="Dev AAP"),
        },
        {
            "connection_name": "Prod AAP",
            "url": "https://prod.example.com",
            "name_prefix": "prod_",
            "org_ids": [2],
            "src_client": SimpleNamespace(label="Prod AAP"),
        },
    ]

    job = SimpleNamespace(result=None, status="running", _resume_event=asyncio.Event())

    async def wait_for_resume() -> None:
        await job._resume_event.wait()
        job._resume_event.clear()

    job.wait_for_resume = wait_for_resume
    job._resume_event.set()

    events: list[dict] = []
    logs: list[str] = []

    class FakeSvc:
        def persist_job(self, j):
            return None

    await planner._handle_credential_pause(
        job,
        FakeSvc(),
        created_creds,
        sources,
        "plan-x",
        "phase-x",
        events.append,
        logs.append,
    )

    assert sorted(calls) == [("Dev AAP", ["10"]), ("Prod AAP", ["10"])]
    pause = next(e for e in events if e.get("_event") == "credential_pause")
    assert len(pause["credentials"]) == 2
    by_source = {c["source"]: c for c in pause["credentials"]}
    assert by_source["Dev AAP"]["name"] == "dev_Shared"
    assert by_source["Dev AAP"]["name_prefix"] == "dev_"
    assert by_source["Prod AAP"]["name"] == "prod_Shared"
    assert job.result["credential_review"][0]["source"] in {"Dev AAP", "Prod AAP"}


@pytest.mark.asyncio
async def test_credential_pause_still_pauses_on_source_label_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source-label mismatches must not skip the secret-update pause."""
    monkeypatch.setattr(
        planner,
        "_build_credential_review",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    created_creds = [
        {
            "name": "dev_Machine",
            "credential_type": "Machine",
            "organization": "Default",
            "source_id": "42",
            "source": "https://dev.example.com",
            "name_prefix": "dev_",
        }
    ]
    # connection_name differs from stamped source — previously skipped the pause
    sources = [
        {
            "connection_name": "Dev AAP",
            "url": "https://dev.example.com",
            "name_prefix": "dev_",
            "org_ids": [1],
            "src_client": SimpleNamespace(),
        }
    ]

    job = SimpleNamespace(result=None, status="running", _resume_event=asyncio.Event())

    async def wait_for_resume() -> None:
        await job._resume_event.wait()
        job._resume_event.clear()

    job.wait_for_resume = wait_for_resume
    job._resume_event.set()
    events: list[dict] = []
    logs: list[str] = []

    class FakeSvc:
        def persist_job(self, j):
            return None

    await planner._handle_credential_pause(
        job,
        FakeSvc(),
        created_creds,
        sources,
        "plan-x",
        "phase-x",
        events.append,
        logs.append,
    )

    pause = next(e for e in events if e.get("_event") == "credential_pause")
    assert pause["credentials"][0]["name"] == "dev_Machine"
    assert job.status == "running"
    assert any("Paused" in line for line in logs)


@pytest.mark.asyncio
async def test_migrate_tracks_skipped_credentials_for_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Already-migrated credentials must still be listed for the secret pause."""

    class FakeExporter:
        async def export(self):
            yield {
                "id": 42,
                "name": "Machine",
                "organization": 1,
                "summary_fields": {
                    "credential_type": {"name": "Machine"},
                    "organization": {"name": "Default"},
                },
            }

    class FakeImporter:
        async def import_resource(self, resource_type, source_id, data):
            return {"id": 99, "name": data["name"], "_already_migrated": True}

    monkeypatch.setattr(
        "aap_migration.migration.exporter.create_exporter", lambda **kwargs: FakeExporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.importer.create_importer", lambda **kwargs: FakeImporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.transformer.create_transformer", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "aap_migration.resources.RESOURCE_REGISTRY",
        {
            "credentials": SimpleNamespace(
                description="Credentials",
                has_transformer=False,
            )
        },
    )

    created_creds: list[dict[str, str]] = []
    events: list[dict] = []
    sources = [
        {
            "src_client": object(),
            "state": object(),
            "migration_config": SimpleNamespace(performance=None, resource_mappings={}),
            "name_prefix": "dev_",
            "connection_name": "Dev AAP",
            "org_ids": [1],
            "url": "https://dev.example.com",
        }
    ]

    created, skipped, failed, exported = await planner._migrate_resource_type(
        "credentials",
        sources,
        object(),
        1,
        events.append,
        lambda _line: None,
        created_creds,
    )

    assert (created, skipped, failed, exported) == (0, 1, 0, 1)
    assert created_creds[0]["name"] == "dev_Machine"
    assert created_creds[0]["source"] == "Dev AAP"
    assert any(e.get("result") == "exists" for e in events)
    exists_evt = next(e for e in events if e.get("result") == "exists")
    assert "Already migrated" in exists_evt.get("detail", "") or exists_evt.get("detail")


@pytest.mark.asyncio
async def test_migrate_logs_skip_reason_for_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipped resources must emit a human-readable reason in events and logs."""
    from aap_migration.migration.transformer import SkipResourceError

    class FakeExporter:
        async def export(self):
            yield {
                "id": 42,
                "name": "Vault Cred",
                "organization": 1,
                "summary_fields": {
                    "credential_type": {"name": "Vault"},
                    "organization": {"name": "Default"},
                },
            }

    class FakeTransformer:
        def transform_resource(self, resource_type, data, validate=True):
            raise SkipResourceError(
                "Skipping 'Vault Cred': required credential_types "
                "(source id 9) was not migrated — include that dependency "
                "in the plan or migrate it first",
                resource_type="credentials",
                source_id=42,
                missing_dependency="credential_types:9",
            )

    class FakeImporter:
        async def import_resource(self, *args, **kwargs):
            raise AssertionError("import should not run after transform skip")

    monkeypatch.setattr(
        "aap_migration.migration.exporter.create_exporter", lambda **kwargs: FakeExporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.importer.create_importer", lambda **kwargs: FakeImporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.transformer.create_transformer",
        lambda **kwargs: FakeTransformer(),
    )
    monkeypatch.setattr(
        "aap_migration.resources.RESOURCE_REGISTRY",
        {
            "credentials": SimpleNamespace(
                description="Credentials",
                has_transformer=True,
            )
        },
    )

    events: list[dict] = []
    logs: list[str] = []
    sources = [
        {
            "src_client": object(),
            "state": object(),
            "migration_config": SimpleNamespace(performance=None, resource_mappings={}),
            "name_prefix": "",
            "connection_name": "Dev AAP",
            "org_ids": [1],
            "url": "https://dev.example.com",
        }
    ]

    created, skipped, failed, exported = await planner._migrate_resource_type(
        "credentials",
        sources,
        object(),
        1,
        events.append,
        logs.append,
        [],
    )

    assert (created, skipped, failed, exported) == (0, 1, 0, 0)
    skip_evt = next(e for e in events if e.get("result") == "skipped")
    assert "credential_types" in skip_evt["detail"]
    assert any("Skipped credentials/Vault Cred" in line for line in logs)
    assert any("credential_types" in line for line in logs)


@pytest.mark.asyncio
async def test_migrate_resource_type_applies_name_prefix_and_tags_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeExporter:
        async def export(self):
            yield {
                "id": 42,
                "name": "Machine",
                "organization": 1,
                "summary_fields": {
                    "credential_type": {"name": "Machine"},
                    "organization": {"name": "Default"},
                },
            }

    class FakeImporter:
        async def import_resource(self, resource_type, source_id, data):
            assert data["name"] == "dev_Machine"
            return True

    monkeypatch.setattr(
        "aap_migration.migration.exporter.create_exporter", lambda **kwargs: FakeExporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.importer.create_importer", lambda **kwargs: FakeImporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.transformer.create_transformer", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "aap_migration.resources.RESOURCE_REGISTRY",
        {
            "credentials": SimpleNamespace(
                description="Credentials",
                has_transformer=False,
            )
        },
    )

    created_creds: list[dict[str, str]] = []
    events: list[dict] = []
    sources = [
        {
            "src_client": object(),
            "state": object(),
            "migration_config": SimpleNamespace(performance=None, resource_mappings={}),
            "name_prefix": "dev_",
            "connection_name": "Dev AAP",
            "org_ids": [1],
            "url": "https://dev.example.com",
        }
    ]

    created, skipped, failed, exported = await planner._migrate_resource_type(
        "credentials",
        sources,
        object(),
        1,
        events.append,
        lambda _line: None,
        created_creds,
    )

    assert (created, skipped, failed, exported) == (1, 0, 0, 1)
    assert created_creds == [
        {
            "name": "dev_Machine",
            "credential_type": "Machine",
            "organization": "Default",
            "source_id": "42",
            "source": "Dev AAP",
            "name_prefix": "dev_",
        }
    ]


@pytest.mark.asyncio
async def test_migrate_skips_name_prefix_for_managed_credential_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported_names: list[str] = []

    class FakeExporter:
        async def export(self):
            yield {"id": 1, "name": "Machine", "managed": True}
            yield {"id": 50, "name": "MyCustomVault", "managed": False}

    class FakeImporter:
        async def import_resource(self, resource_type, source_id, data):
            imported_names.append(data["name"])
            return True

    monkeypatch.setattr(
        "aap_migration.migration.exporter.create_exporter", lambda **kwargs: FakeExporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.importer.create_importer", lambda **kwargs: FakeImporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.transformer.create_transformer", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "aap_migration.resources.RESOURCE_REGISTRY",
        {
            "credential_types": SimpleNamespace(
                description="Credential Types",
                has_transformer=False,
            )
        },
    )

    sources = [
        {
            "src_client": object(),
            "state": object(),
            "migration_config": SimpleNamespace(performance=None, resource_mappings={}),
            "name_prefix": "dev_",
            "connection_name": "Dev AAP",
            "org_ids": [],
            "url": "https://dev.example.com",
        }
    ]

    await planner._migrate_resource_type(
        "credential_types",
        sources,
        object(),
        1,
        lambda _e: None,
        lambda _line: None,
        [],
    )

    assert imported_names == ["Machine", "dev_MyCustomVault"]


@pytest.mark.asyncio
async def test_migrate_skips_name_prefix_for_managed_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported_names: list[str] = []

    class FakeExporter:
        async def export(self):
            yield {"id": 1, "name": "Ansible Galaxy", "managed": True}
            yield {"id": 2, "name": "My SCM Cred", "managed": False}

    class FakeImporter:
        async def import_resource(self, resource_type, source_id, data):
            imported_names.append(data["name"])
            return True

    monkeypatch.setattr(
        "aap_migration.migration.exporter.create_exporter", lambda **kwargs: FakeExporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.importer.create_importer", lambda **kwargs: FakeImporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.transformer.create_transformer", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "aap_migration.resources.RESOURCE_REGISTRY",
        {
            "credentials": SimpleNamespace(
                description="Credentials",
                has_transformer=False,
            )
        },
    )

    sources = [
        {
            "src_client": object(),
            "state": object(),
            "migration_config": SimpleNamespace(performance=None, resource_mappings={}),
            "name_prefix": "dev_",
            "connection_name": "Dev AAP",
            "org_ids": [],
            "url": "https://dev.example.com",
        }
    ]

    await planner._migrate_resource_type(
        "credentials",
        sources,
        object(),
        1,
        lambda _e: None,
        lambda _line: None,
        [],
    )

    assert imported_names == ["Ansible Galaxy", "dev_My SCM Cred"]


@pytest.mark.asyncio
async def test_migrate_resource_type_honors_excluded_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported_ids: list[int] = []

    class FakeExporter:
        async def export(self):
            yield {"id": 1, "name": "Keep", "organization": 1}
            yield {"id": 2, "name": "Drop", "organization": 1}

    class FakeImporter:
        async def import_resource(self, resource_type, source_id, data):
            imported_ids.append(source_id)
            return True

    monkeypatch.setattr(
        "aap_migration.migration.exporter.create_exporter", lambda **kwargs: FakeExporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.importer.create_importer", lambda **kwargs: FakeImporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.transformer.create_transformer", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "aap_migration.resources.RESOURCE_REGISTRY",
        {
            "inventories": SimpleNamespace(
                description="Inventories",
                has_transformer=False,
            )
        },
    )

    events: list[dict] = []
    sources = [
        {
            "src_client": object(),
            "state": object(),
            "migration_config": SimpleNamespace(performance=None, resource_mappings={}),
            "name_prefix": "",
            "connection_name": "Dev AAP",
            "org_ids": [1],
            "url": "https://dev.example.com",
            "excluded_ids": {"inventories": [2]},
        }
    ]

    created, skipped, failed, exported = await planner._migrate_resource_type(
        "inventories",
        sources,
        object(),
        1,
        events.append,
        lambda _line: None,
        [],
    )

    assert (created, skipped, failed, exported) == (1, 1, 0, 1)
    assert imported_ids == [1]
    assert any(
        e.get("result") == "skipped"
        and "Excluded" in e.get("detail", "")
        and "(1 resources)" in e.get("name", "")
        for e in events
    )


@pytest.mark.asyncio
async def test_migrate_resource_type_short_circuits_fully_excluded_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export_calls = {"n": 0}

    class FakeExporter:
        async def export(self):
            export_calls["n"] += 1
            yield {"id": 1, "name": "should-not-export"}

    monkeypatch.setattr(
        "aap_migration.migration.exporter.create_exporter", lambda **kwargs: FakeExporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.importer.create_importer", lambda **kwargs: object()
    )
    monkeypatch.setattr(
        "aap_migration.migration.transformer.create_transformer", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "aap_migration.resources.RESOURCE_REGISTRY",
        {
            "hosts": SimpleNamespace(
                description="Hosts",
                has_transformer=False,
            )
        },
    )

    logs: list[str] = []
    events: list[dict] = []
    sources = [
        {
            "src_client": object(),
            "state": object(),
            "migration_config": SimpleNamespace(performance=None, resource_mappings={}),
            "name_prefix": "",
            "connection_name": "Dev AAP",
            "org_ids": [],
            "url": "https://dev.example.com",
            "excluded_ids": {"hosts": [1, 2, 3]},
            "preview_resources": {
                "hosts": [
                    {"source_id": 1},
                    {"source_id": 2},
                    {"source_id": 3},
                ]
            },
        }
    ]

    created, skipped, failed, exported = await planner._migrate_resource_type(
        "hosts",
        sources,
        object(),
        1,
        events.append,
        logs.append,
        [],
    )

    assert (created, skipped, failed, exported) == (0, 3, 0, 0)
    assert export_calls["n"] == 0
    assert any("all 3 resource(s) excluded by user" in line for line in logs)
    assert any(e.get("_event") == "phase_complete" and e.get("skipped") == 3 for e in events)


@pytest.mark.asyncio
async def test_migrate_resource_type_skips_memberships_for_excluded_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported: list[dict] = []

    class FakeExporter:
        async def export(self):
            yield {"host_id": 1, "inventory_id": 10, "host_name": "keep"}
            yield {"host_id": 2, "inventory_id": 10, "host_name": "drop"}

    class FakeImporter:
        async def import_resource(self, resource=None, xformed=None):
            imported.append(resource or {})
            return {"status": "created"}

    monkeypatch.setattr(
        "aap_migration.migration.exporter.create_exporter", lambda **kwargs: FakeExporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.importer.create_importer", lambda **kwargs: FakeImporter()
    )
    monkeypatch.setattr(
        "aap_migration.migration.transformer.create_transformer", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "aap_migration.resources.RESOURCE_REGISTRY",
        {
            "host_inventory_memberships": SimpleNamespace(
                description="Memberships",
                has_transformer=False,
            )
        },
    )

    events: list[dict] = []
    sources = [
        {
            "src_client": object(),
            "state": object(),
            "migration_config": SimpleNamespace(performance=None, resource_mappings={}),
            "name_prefix": "",
            "connection_name": "Dev AAP",
            "org_ids": [],
            "url": "https://dev.example.com",
            "excluded_ids": {"hosts": [2]},
        }
    ]

    created, skipped, failed, exported = await planner._migrate_resource_type(
        "host_inventory_memberships",
        sources,
        object(),
        1,
        events.append,
        lambda _line: None,
        [],
    )

    assert (created, skipped, failed, exported) == (1, 1, 0, 1)
    assert [m["host_id"] for m in imported] == [1]
    assert any(
        e.get("result") == "skipped"
        and "Host excluded" in e.get("detail", "")
        and "(1 resources)" in e.get("name", "")
        for e in events
    )


def test_resource_in_orgs_tightened_filter() -> None:
    # Org-scoped inventory outside selected orgs is excluded
    assert (
        planner._resource_in_orgs(
            "inventories",
            {"id": 9, "organization": 99, "name": "Other"},
            9,
            [1, 2],
        )
        is False
    )
    # Inventory in selected org is included
    assert (
        planner._resource_in_orgs(
            "inventories",
            {"id": 9, "organization": 1, "name": "Mine"},
            9,
            [1, 2],
        )
        is True
    )
    # Org-less inventory is NOT auto-included (unlike previous permissive behavior)
    assert (
        planner._resource_in_orgs(
            "inventories",
            {"id": 9, "organization": None, "name": "NoOrg"},
            9,
            [1, 2],
        )
        is False
    )
    # Global credential types still included
    assert (
        planner._resource_in_orgs(
            "credential_types",
            {"id": 1, "name": "Machine", "managed": True},
            1,
            [1, 2],
        )
        is True
    )
    # Org-less credentials still included (user/team owned)
    assert (
        planner._resource_in_orgs(
            "credentials",
            {"id": 3, "organization": None, "name": "UserCred"},
            3,
            [1, 2],
        )
        is True
    )


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
