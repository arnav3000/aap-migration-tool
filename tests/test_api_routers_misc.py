from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException

from aap_migration.api.routers import analysis, jobs, resources, sizing
from aap_migration.api.schemas import AnalysisRunRequest
from aap_migration.resources import RESOURCE_REGISTRY


class FakeJob:
    def __init__(self, status="running", result=None) -> None:
        self.status = status
        self.result = result
        self.error = None
        self._html_report = None

    def to_dict(self) -> dict:
        return {"status": self.status, "result": self.result}


class FakeJobService:
    def __init__(self) -> None:
        self.jobs: dict[str, FakeJob] = {}
        self.started = []
        self.cancel_result = True
        self.resume_result = True

    def list_jobs(self):
        return [{"id": "job-1"}]

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    def cancel_job(self, job_id: str) -> bool:
        return self.cancel_result

    def resume_job(self, job_id: str) -> bool:
        return self.resume_result

    def start_job(self, name, job_type, callback):
        self.started.append((name, job_type, callback))
        return f"{job_type}-job"


class FakeSession:
    def __init__(self, record=None) -> None:
        self.record = record
        self.committed = False
        self.closed = False

    def get(self, model, key):
        return self.record

    def close(self) -> None:
        self.closed = True

    def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_jobs_router_resume_and_exports(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = FakeJobService()
    waiting_job = FakeJob(
        status="waiting_for_input",
        result={
            "_paused_plan_id": "plan-1",
            "_paused_phase_id": "phase-1",
            "credential_review": [
                {
                    "name": "Machine",
                    "credential_type": "ssh",
                    "organization": "Default",
                    "used_by": [{"resource_type": "job_template", "resource_name": "Deploy"}],
                }
            ],
        },
    )
    svc.jobs["job-1"] = waiting_job
    monkeypatch.setattr(jobs, "get_job_service", lambda: svc)

    assert jobs.list_jobs() == [{"id": "job-1"}]
    assert jobs.get_job("job-1") == waiting_job.to_dict()
    assert jobs.cancel_job("job-1") == {"status": "cancelled"}
    assert await jobs.resume_job("job-1") == {"status": "running"}

    svc.resume_result = False
    record = SimpleNamespace(status="waiting_for_input", error="old")
    session = FakeSession(record)
    state = SimpleNamespace(db_session_factory=lambda: session)
    monkeypatch.setattr("aap_migration.api.dependencies.get_app_state", lambda: state)
    fake_planner = ModuleType("aap_migration.api.routers.planner")

    async def execute_phase(plan_id, phase_id, db):
        assert (plan_id, phase_id) == ("plan-1", "phase-1")
        assert db is session
        return SimpleNamespace(job_id="new-job")

    fake_planner.execute_phase = execute_phase
    monkeypatch.setitem(sys.modules, "aap_migration.api.routers.planner", fake_planner)

    assert await jobs.resume_job("job-1") == {"status": "running", "new_job_id": "new-job"}
    assert record.status == "resumed"
    assert record.error is None
    assert session.committed is True

    creds = jobs.get_job_credentials("job-1")
    assert creds[0]["name"] == "Machine"
    csv_response = jobs.get_job_credentials_csv("job-1")
    csv_body = b"".join(
        [
            chunk if isinstance(chunk, bytes) else chunk.encode()
            async for chunk in csv_response.body_iterator
        ]
    ).decode()
    assert "Credential Name,Credential Type,Organization,Used By Type,Used By Name" in csv_body
    assert "Machine,ssh,Default,job_template,Deploy" in csv_body


def test_jobs_router_error_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = FakeJobService()
    svc.jobs["job-2"] = FakeJob(status="completed", result={})
    monkeypatch.setattr(jobs, "get_job_service", lambda: svc)

    with pytest.raises(HTTPException, match="Job not found"):
        jobs.get_job("missing")

    with pytest.raises(HTTPException, match="Job not found"):
        jobs.cancel_job("missing")

    with pytest.raises(HTTPException, match="Job not found"):
        jobs.get_job_credentials_csv("missing")


@pytest.mark.asyncio
async def test_resources_router_success_and_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = SimpleNamespace(id="conn-1")
    monkeypatch.setattr(
        resources.ConnectionService,
        "get",
        lambda db, conn_id: conn if conn_id == "conn-1" else None,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.fail_orgs = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, endpoint, params=None):
            if endpoint == "users/":
                raise RuntimeError("boom")
            return {"count": 3}

        async def get_paginated(self, endpoint, page_size=200):
            if endpoint == "organizations/":
                if self.fail_orgs:
                    raise RuntimeError("bad orgs")
                return [{"id": 1, "name": "Default", "description": "Org"}]
            if endpoint == "teams/":
                raise RuntimeError("bad teams")
            return [{"id": 1, "name": "TeamA"}]

    fake_client = FakeClient()
    monkeypatch.setattr(
        resources.ConnectionService, "build_source_client", lambda conn: fake_client
    )

    types_result = await resources.list_resource_types("conn-1", db=None)
    assert len(types_result) == len(RESOURCE_REGISTRY)
    assert next(item for item in types_result if item["name"] == "users")["count"] == 0
    assert next(item for item in types_result if item["name"] == "organizations")["count"] == 3

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise TimeoutError()

    monkeypatch.setattr(resources.asyncio, "wait_for", fake_wait_for)
    fallback = await resources.list_resource_types("conn-1", db=None)
    assert all(item["count"] == -1 for item in fallback)

    orgs = await resources.list_organizations("conn-1", db=None)
    assert orgs == [{"id": 1, "name": "Default", "description": "Org"}]

    fake_client.fail_orgs = True
    with pytest.raises(HTTPException, match="Failed to query organizations"):
        await resources.list_organizations("conn-1", db=None)

    with pytest.raises(HTTPException, match="Unknown resource type"):
        await resources.list_resources("conn-1", "does-not-exist", db=None)

    with pytest.raises(HTTPException, match="Failed to query AAP"):
        await resources.list_resources("conn-1", "teams", db=None)


def test_sizing_router_calculate_and_dynamic(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCalculator:
        def validate_input(self, key, value):
            return ["watch forks"] if key == "forks_observed" else []

        def generate_sizing_recommendation(self, metrics, deployment_target):
            assert deployment_target == "ocp"
            assert "deployment_target" not in metrics
            return {
                "components": {
                    "automation_controller_execution_plane": {"replicas": 2},
                    "automation_controller_control_plane": {"replicas": 1},
                    "database": {"storage_gb": 20},
                    "automation_hub": {"enabled": True},
                    "platform_gateway": {"replicas": 1},
                    "event_driven_ansible": {"replicas": 0},
                    "redis": {"replicas": 1},
                },
                "deployment": {"kind": "ocp"},
                "warnings": ["generated warning"],
            }

        def validate_results(self, execution, controller, database):
            return ["validate warning"]

    monkeypatch.setattr(sizing, "AAP26SizingCalculator", FakeCalculator)
    result = sizing.calculate_sizing(
        sizing.SizingRequest(managed_hosts=100, playbooks_per_day_peak=50, forks_observed=42)
    )
    assert result.execution_nodes == {"replicas": 2}
    assert result.warnings == ["watch forks", "generated warning"]
    assert result.validation_warnings == ["validate warning"]

    conn = SimpleNamespace(
        token="encrypted",
        type="awx",
        url="https://tower.example.com/api/v2",
        verify_ssl=False,
        api_prefix=None,
    )
    monkeypatch.setattr(
        sizing.ConnectionService, "get", lambda db, conn_id: conn if conn_id == "conn-1" else None
    )
    monkeypatch.setattr(sizing, "decrypt_token", lambda token: "secret" if token else "")

    fake_dynamic = ModuleType("aap_migration.sizing.dynamic")

    def run_dynamic(**kwargs):
        assert kwargs["api_prefix"] == ""
        assert kwargs["auth_scheme"] == "Token"
        return {
            "mode": "dynamic",
            "deployment_target": kwargs["deployment_target"],
            "source_observed": {"jobs": 3},
            "derived_inputs": {"forks": 10},
            "headroom_multiplier": 1.3,
            "recommendation": {"controller": 2},
        }

    fake_dynamic.calculate_dynamic_sizing = run_dynamic
    monkeypatch.setitem(sys.modules, "aap_migration.sizing.dynamic", fake_dynamic)

    dynamic = sizing.calculate_dynamic_sizing(
        sizing.DynamicSizingRequest(
            connection_id="conn-1", history_days=7, deployment_target="containerized"
        ),
        db=None,
    )
    assert dynamic.mode == "dynamic"
    assert dynamic.recommendation == {"controller": 2}

    monkeypatch.setattr(sizing, "decrypt_token", lambda token: "")
    with pytest.raises(HTTPException, match="no authentication token"):
        sizing.calculate_dynamic_sizing(
            sizing.DynamicSizingRequest(connection_id="conn-1"), db=None
        )

    monkeypatch.setattr(sizing.ConnectionService, "get", lambda db, conn_id: None)
    with pytest.raises(HTTPException, match="Connection not found"):
        sizing.calculate_dynamic_sizing(
            sizing.DynamicSizingRequest(connection_id="missing"), db=None
        )


@pytest.mark.asyncio
async def test_analysis_router_run_and_exports(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = FakeJobService()
    monkeypatch.setattr(analysis, "get_job_service", lambda: svc)
    conn = SimpleNamespace(url="https://source.example.com")
    monkeypatch.setattr(
        analysis.ConnectionService,
        "get",
        lambda db, connection_id: conn if connection_id == "conn-1" else None,
    )
    monkeypatch.setattr(
        analysis.ConnectionService,
        "build_instance_config",
        lambda conn: SimpleNamespace(url=conn.url),
    )
    monkeypatch.setattr(analysis.ConnectionService, "_auth_scheme", lambda conn: "Token")

    fake_client_module = ModuleType("aap_migration.client.aap_source_client")

    class FakeSourceClient:
        def __init__(self, config, auth_scheme=None):
            self.config = config
            self.auth_scheme = auth_scheme

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    fake_client_module.AAPSourceClient = FakeSourceClient
    monkeypatch.setitem(sys.modules, "aap_migration.client.aap_source_client", fake_client_module)

    fake_analyzer_module = ModuleType("aap_migration.analysis.dependency_analyzer")

    class FakeAnalyzer:
        def __init__(self, source_client, progress_callback):
            self.progress_callback = progress_callback

        async def analyze_all_organizations(self):
            self.progress_callback(1, 2, "Scanning")
            return SimpleNamespace(
                analysis_date=datetime(2026, 5, 18, tzinfo=UTC),
                source_url="https://source.example.com",
                total_organizations=2,
                analyzed_organizations=2,
                independent_orgs=["A"],
                dependent_orgs=["B"],
                migration_order=["A", "B"],
                migration_phases=[["A"], {"phase": 2, "orgs": ["B"], "description": "Phase 2"}],
                global_resources={"inventories": [1, 2]},
                total_duplicates=1,
                average_quality_score=88.5,
                org_reports={
                    "A": SimpleNamespace(
                        org_id=1,
                        resource_count=5,
                        has_cross_org_deps=False,
                        can_migrate_standalone=True,
                        required_migrations_before=[],
                        dependencies={},
                        resources={"projects": [1, 2]},
                        quality_report=None,
                    ),
                    "B": SimpleNamespace(
                        org_id=2,
                        resource_count=4,
                        has_cross_org_deps=True,
                        can_migrate_standalone=False,
                        required_migrations_before=["A"],
                        dependencies={},
                        resources={"users": [1]},
                        quality_report=None,
                    ),
                },
            )

    fake_analyzer_module.CrossOrgDependencyAnalyzer = FakeAnalyzer
    monkeypatch.setitem(
        sys.modules, "aap_migration.analysis.dependency_analyzer", fake_analyzer_module
    )

    fake_html_module = ModuleType("aap_migration.analysis.html_report")
    fake_html_module.generate_html_report = lambda report: "<html>report</html>"
    monkeypatch.setitem(sys.modules, "aap_migration.analysis.html_report", fake_html_module)

    response = await analysis.run_analysis(AnalysisRunRequest(connection_id="conn-1"), db=None)
    assert response.job_id == "analysis-job"
    _, _, callback = svc.started[0]
    job = FakeJob()
    logs = []
    result = await callback(job, logs.append)
    assert result["analysis_date"] == "2026-05-18T00:00:00+00:00"
    assert result["organizations"]["B"]["blocks"] == []
    assert job._html_report == "<html>report</html>"
    assert any("Analysis complete" in message for message in logs)

    completed_job = FakeJob(
        status="completed",
        result={"migration_phases": [{"orgs": {"orgs": ["A"]}}], "answer": 42},
    )
    completed_job._html_report = "<html>report</html>"
    svc.jobs["analysis-job"] = completed_job
    data = analysis.get_analysis_result("analysis-job")
    assert data["data"]["migration_phases"] == [{"orgs": ["A"]}]

    json_response = analysis.export_analysis_json("analysis-job")
    assert json.loads(json_response.body.decode())["answer"] == 42
    html_response = analysis.export_analysis_html("analysis-job")
    assert html_response.body.decode() == "<html>report</html>"

    pending_job = FakeJob(status="running")
    svc.jobs["pending"] = pending_job
    with pytest.raises(HTTPException, match="Analysis not yet complete"):
        analysis.export_analysis_json("pending")
