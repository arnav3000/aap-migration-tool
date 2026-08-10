from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from types import SimpleNamespace

import aap_migration.api.models as api_models
from aap_migration.analysis.dependency_analyzer import (
    GlobalDependencyReport,
    OrgDependencyReport,
    ResourceDependency,
)


def _build_report() -> GlobalDependencyReport:
    dep = ResourceDependency("projects", 7, "Shared Project", "SharedOrg")
    dep.add_usage("job_templates", 12, "JT")
    dependent = OrgDependencyReport(
        org_name="DependentOrg",
        org_id=2,
        resource_count=3,
        has_cross_org_deps=True,
        dependencies={"SharedOrg": [dep]},
        can_migrate_standalone=False,
        required_migrations_before=["SharedOrg"],
        resources={"projects": [{"id": 7}], "job_templates": [{"id": 12}]},
    )
    dependent.quality_report = SimpleNamespace(
        quality_score=88,
        duplicate_count=1,
        duplicates=[
            SimpleNamespace(
                name="dup",
                resource_type="projects",
                count=2,
                ids=[1, 2],
                severity="high",
                impact="collision",
                recommendation="rename",
            )
        ],
        naming_pattern=SimpleNamespace(
            dominant_pattern="snake_case",
            consistency_score=95,
            total_resources=4,
            case_style="snake",
            prefixes=["ops_"],
            separators=["_"],
            violations=["BadName"],
        ),
    )
    independent = OrgDependencyReport(
        org_name="SharedOrg",
        org_id=1,
        resource_count=1,
        has_cross_org_deps=False,
        dependencies={},
        can_migrate_standalone=True,
        required_migrations_before=[],
        resources={"projects": [{"id": 7}]},
    )
    report = GlobalDependencyReport(
        analysis_date=datetime(2026, 1, 1, tzinfo=UTC),
        source_url="https://source.example.com",
        total_organizations=2,
        analyzed_organizations=["SharedOrg", "DependentOrg"],
        independent_orgs=["SharedOrg"],
        dependent_orgs=["DependentOrg"],
        org_reports={"SharedOrg": independent, "DependentOrg": dependent},
        migration_order=["SharedOrg", "DependentOrg"],
        migration_phases=[["SharedOrg"], ["DependentOrg"]],
    )
    report.global_resources = {"projects": [{"id": 7}]}
    report.total_duplicates = 1
    report.average_quality_score = 91.5
    report.get_quality_summary = lambda: {"overall": "good"}  # type: ignore[attr-defined]
    return report


def test_analysis_service_serialization_and_background_run(monkeypatch) -> None:
    class DummyJob:
        id = "id"

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.finished_at = None
            self.error = None
            self.job_metadata = None
            self.output = None

    api_models.Job = DummyJob
    analysis_service = importlib.import_module("aap_migration.api.services.analysis_service")
    analysis_service = importlib.reload(analysis_service)

    monkeypatch.setattr(
        "aap_migration.analysis.dependency_graph.detect_cycles",
        lambda graph: [["DependentOrg", "SharedOrg"]],
    )
    serialized = analysis_service._serialize_report(_build_report())
    assert serialized["circular_dependencies"] == [["DependentOrg", "SharedOrg"]]
    assert serialized["quality_summary"] == {"overall": "good"}
    assert serialized["organizations"]["DependentOrg"]["quality"]["quality_score"] == 88

    jobs = {}

    class FakeQuery:
        def __init__(self, store):
            self.store = store
            self.current_id = None

        def filter(self, _expr):
            return self

        def first(self):
            return next(reversed(self.store.values()), None) if self.store else None

    class FakeSession:
        def add(self, job):
            jobs[job.id] = job

        def commit(self):
            return None

        def close(self):
            return None

        def query(self, _model):
            return FakeQuery(jobs)

    job_service = SimpleNamespace(
        registered=[],
        logs={},
        register_job=lambda job_id: job_service.registered.append(job_id),
        append_log=lambda job_id, msg: job_service.logs.setdefault(job_id, []).append(msg),
        get_logs_since=lambda job_id, offset: job_service.logs.get(job_id, []),
    )

    monkeypatch.setattr("aap_migration.api.crypto.decrypt_token", lambda token: "plain-token")
    monkeypatch.setattr(
        "aap_migration.client.aap_source_client.AAPSourceClient",
        lambda config=None: SimpleNamespace(config=config),
    )
    monkeypatch.setattr(
        "aap_migration.config.AAPInstanceConfig", lambda **kwargs: SimpleNamespace(**kwargs)
    )

    class GoodAnalyzer:
        def __init__(self, client):
            self.client = client

        async def analyze_all_organizations(self):
            return _build_report()

    class BadAnalyzer:
        def __init__(self, client):
            self.client = client

        async def analyze_all_organizations(self):
            raise RuntimeError("analysis boom")

    monkeypatch.setattr(
        analysis_service.asyncio,
        "run_coroutine_threadsafe",
        lambda coro, loop: asyncio.new_event_loop().run_until_complete(coro),
    )
    monkeypatch.setattr(
        "aap_migration.analysis.dependency_analyzer.CrossOrgDependencyAnalyzer", GoodAnalyzer
    )

    service = analysis_service.AnalysisService(
        job_service, lambda: FakeSession(), loop=asyncio.new_event_loop()
    )
    job_id = service.start_analysis(
        SimpleNamespace(
            id="conn-1",
            url="https://source.example.com",
            token="encrypted",
            verify_ssl=True,
            api_prefix="/api/controller",
        )
    )
    assert job_id in jobs
    assert jobs[job_id].status == "completed"
    assert jobs[job_id].job_metadata["total_organizations"] == 2

    monkeypatch.setattr(
        "aap_migration.analysis.dependency_analyzer.CrossOrgDependencyAnalyzer", BadAnalyzer
    )
    failed_job_id = service.start_analysis(
        SimpleNamespace(
            id="conn-2",
            url="https://source.example.com",
            token="encrypted",
            verify_ssl=True,
            api_prefix="",
        )
    )
    assert jobs[failed_job_id].status == "failed"
    assert jobs[failed_job_id].error == "analysis boom"
