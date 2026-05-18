from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest


class DummyConn:
    pass


class DummyJobModel:
    id = "id"

    def __init__(self, id=None, type=None, connection_id=None, status=None):
        self.id = id
        self.type = type
        self.connection_id = connection_id
        self.status = status
        self.finished_at = None
        self.error = None
        self.output = None
        self.job_metadata = None


import aap_migration.api.models as api_models  # noqa: E402
import aap_migration.api.schemas as api_schemas  # noqa: E402

api_models.Job = DummyJobModel
api_schemas.MigrationPreviewResponse = type(
    "MigrationPreviewResponse",
    (),
    {"__init__": lambda self, **payload: self.__dict__.update(payload)},
)

import aap_migration.api.services.analysis_service as analysis_module  # noqa: E402
import aap_migration.api.services.migration_service as migration_module  # noqa: E402
import aap_migration.api.services.operation_service as operation_module  # noqa: E402
import aap_migration.api.services.platform_adapter as platform_module  # noqa: E402
from aap_migration.api.services.analysis_service import (  # noqa: E402
    AnalysisService,
    _serialize_report,
)
from aap_migration.api.services.engine_adapter import (  # noqa: E402
    build_migration_config,
    connection_to_aap_config,
)
from aap_migration.api.services.migration_service import MigrationService  # noqa: E402
from aap_migration.api.services.operation_service import OperationService  # noqa: E402


class FakeQuery:
    def __init__(self, result):
        self.result = result

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.result


class FakeSession:
    def __init__(self, result=None):
        self.result = result
        self.added = []
        self.commits = 0
        self.closed = False

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def query(self, model):
        return FakeQuery(self.result)

    def close(self):
        self.closed = True


class FakeSessionFactory:
    def __init__(self, result=None):
        self.sessions = []
        self.result = result

    def __call__(self):
        session = FakeSession(self.result)
        self.sessions.append(session)
        return session


class FakeJobService:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []
        self.completed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.tasks: dict[str, asyncio.Task] = {}
        self.registered: list[str] = []

    def append_log(self, job_id: str, msg: str) -> None:
        self.logs.append((job_id, msg))

    def mark_completed(self, job_id: str) -> None:
        self.completed.append(job_id)

    def mark_failed(self, job_id: str, error: str) -> None:
        self.failed.append((job_id, error))

    def get_logs_since(self, job_id: str, offset: int) -> list[str]:
        return [msg for jid, msg in self.logs if jid == job_id][offset:]

    def register_task(self, job_id: str, task) -> None:
        self.tasks[job_id] = task

    def register_job(self, job_id: str) -> None:
        self.registered.append(job_id)


@pytest.mark.asyncio
async def test_migration_and_operation_service_direct_paths(monkeypatch, tmp_path):
    migration_module.Connection = DummyConn
    migration_module.Job = DummyJobModel
    migration_module.MigrationPreviewResponse = type(
        "PreviewResponse",
        (),
        {"__init__": lambda self, **payload: self.__dict__.update(payload)},
    )
    operation_module.Connection = DummyConn
    operation_module.Job = DummyJobModel

    preview_job = SimpleNamespace(
        job_metadata={"resources": {"projects": [{"source_id": 1}, {"source_id": 2}]}}
    )
    session_factory = FakeSessionFactory(preview_job)
    job_service = FakeJobService()
    loop = asyncio.get_running_loop()

    migration_service = MigrationService(job_service, session_factory, loop)
    operation_service = OperationService(job_service, session_factory, loop)

    created_job = migration_service._create_job("preview", "conn-1")
    assert created_job in job_service.registered
    assert session_factory.sessions[0].added[0].type == "preview"

    existing_job = SimpleNamespace(
        status="running", finished_at=None, error=None, output=None, job_metadata=None
    )
    helper_factory = FakeSessionFactory(existing_job)
    helper_service = MigrationService(job_service, helper_factory, loop)
    helper_service._finish_job("job-1", "completed", metadata={"x": 1})
    assert existing_job.status == "completed"
    assert existing_job.job_metadata == {"x": 1}

    preview_factory = FakeSessionFactory(
        SimpleNamespace(status="completed", job_metadata={"resources": {}, "warnings": []})
    )
    preview_reader = MigrationService(job_service, preview_factory, loop)
    status, preview = preview_reader.get_preview("job-2")
    assert status == "completed"
    assert preview.resources == {}

    finished = []
    monkeypatch.setattr(
        migration_service, "_create_job", lambda job_type, connection_id=None: f"{job_type}-job"
    )
    monkeypatch.setattr(
        migration_service,
        "_finish_job",
        lambda job_id, status, error=None, metadata=None: finished.append(
            (job_id, status, error, metadata)
        ),
    )
    monkeypatch.setattr(migration_service, "_get_db_url", lambda: str(tmp_path / "state.db"))

    class FakePlatformAdapter:
        def __init__(self, conn):
            self.conn = conn

        def fetch_all(self, resource_type):
            source = {
                "organizations": [{"id": 1, "name": "Default"}],
                "inventories": [{"id": 2, "name": "InvA", "total_hosts": 2, "total_groups": 1}],
                "users": [{"id": 3, "username": "alice"}],
            }
            dest = {
                "organizations": [{"id": 9, "name": "Default"}],
                "inventories": [],
                "users": [],
            }
            return (
                source.get(resource_type, [])
                if self.conn.name == "Source"
                else dest.get(resource_type, [])
            )

    monkeypatch.setattr(platform_module, "PlatformAdapter", FakePlatformAdapter)

    source = SimpleNamespace(
        id="src",
        name="Source",
        url="https://source.example.com",
        token="",
        verify_ssl=True,
        type="awx",
        api_prefix="/api/v2",
    )
    dest = SimpleNamespace(
        id="dst",
        name="Destination",
        url="https://dest.example.com",
        token="",
        verify_ssl=True,
        type="awx",
        api_prefix="/api/v2",
    )

    preview_job_id = migration_service.start_preview(source, dest)
    await job_service.tasks[preview_job_id]
    assert preview_job_id == "migration-preview-job"
    assert finished[0][1] == "completed"
    assert finished[0][3]["host_counts"] == {"InvA": 2}

    migrate_calls = []

    class FakeSourceClient:
        def __init__(self, config):
            self.config = config

    class FakeTargetClient:
        def __init__(self, config):
            self.config = config

    class FakeState:
        def __init__(self, config):
            self.created = []
            self.skipped = []

        def create_source_mapping(self, resource_type, source_id, source_name=None):
            self.created.append((resource_type, source_id, source_name))

        def mark_skipped(self, resource_type, source_id, reason):
            self.skipped.append((resource_type, source_id, reason))

    class FakeCoordinator:
        def __init__(
            self, config, source_client, target_client, state, enable_progress, show_stats
        ):
            self.state = state

        async def migrate_all(self, skip_phases=None, generate_report=True, report_dir="./reports"):
            migrate_calls.append((skip_phases, generate_report, report_dir))
            return {
                "status": "completed",
                "total_resources_exported": 4,
                "total_resources_imported": 3,
                "total_resources_failed": 0,
                "total_resources_skipped": 1,
            }

    monkeypatch.setattr(
        "aap_migration.api.services.engine_adapter.build_migration_config",
        lambda src, dst, db_url: SimpleNamespace(source="src", target="dst", state="state"),
    )
    monkeypatch.setattr("aap_migration.client.aap_source_client.AAPSourceClient", FakeSourceClient)
    monkeypatch.setattr("aap_migration.client.aap_target_client.AAPTargetClient", FakeTargetClient)
    monkeypatch.setattr("aap_migration.migration.state.MigrationState", FakeState)
    monkeypatch.setattr("aap_migration.migration.coordinator.MigrationCoordinator", FakeCoordinator)

    run_job_id = migration_service.start_run(
        source, dest, "preview-job", exclusions={"projects": [1, 2]}
    )
    await job_service.tasks[run_job_id]
    assert run_job_id == "migration-run-job"
    assert migrate_calls == [(["projects"], True, "./reports")]

    monkeypatch.setattr(
        operation_service, "_create_job", lambda job_type, connection_id: f"{job_type}-job"
    )
    op_finished = []
    monkeypatch.setattr(
        operation_service,
        "_finish_job",
        lambda job_id, status, error=None: op_finished.append((job_id, status, error)),
    )
    monkeypatch.setattr(operation_service, "_get_db_url", lambda: str(tmp_path / "op-state.db"))
    import aap_migration.cli.commands.cleanup as cleanup_module

    monkeypatch.setattr(
        "aap_migration.api.services.engine_adapter.connection_to_aap_config",
        lambda conn: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "aap_migration.config.AAPInstanceConfig", lambda **kwargs: SimpleNamespace(**kwargs)
    )
    monkeypatch.setattr(
        "aap_migration.config.StateConfig", lambda **kwargs: SimpleNamespace(**kwargs)
    )
    monkeypatch.setattr(
        "aap_migration.config.MigrationConfig", lambda **kwargs: SimpleNamespace(**kwargs)
    )
    monkeypatch.setattr("aap_migration.client.aap_target_client.AAPTargetClient", FakeTargetClient)

    async def fake_cancel_all_jobs(client, config):
        return {"cancelled": 1}

    async def fake_delete_resources(client, resource_type, config, skip_default=True):
        if resource_type == "hosts":
            raise RuntimeError("host failure")
        return (1, 0, 0, [])

    monkeypatch.setattr(cleanup_module, "cancel_all_jobs", fake_cancel_all_jobs)
    monkeypatch.setattr(cleanup_module, "delete_resources", fake_delete_resources)

    conn = SimpleNamespace(
        id="conn-1",
        name="Target",
        url="https://target.example.com",
        token="",
        verify_ssl=True,
        type="awx",
        api_prefix="",
    )
    cleanup_job_id = operation_service.start_cleanup(conn)
    await job_service.tasks[cleanup_job_id]
    assert cleanup_job_id == "cleanup-job"
    assert op_finished[0][1] == "completed"

    class ExportPlatformAdapter:
        def __init__(self, conn):
            self.conn = conn

        def fetch_all(self, resource_type):
            if resource_type == "organizations":
                return [{"id": 1, "name": "Default"}]
            if resource_type == "credentials":
                raise RuntimeError("cred export failed")
            return []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(platform_module, "PlatformAdapter", ExportPlatformAdapter)
    export_job_id = operation_service.start_export(conn)
    await job_service.tasks[export_job_id]
    assert export_job_id == "export-job"
    assert (tmp_path / "exports" / "Target" / "organizations" / "organizations_001.json").exists()


@pytest.mark.asyncio
async def test_analysis_service_platform_adapter_and_engine_adapter(monkeypatch):
    analysis_module.Connection = DummyConn
    analysis_module.Job = DummyJobModel

    job_service = FakeJobService()
    loop = asyncio.get_running_loop()
    stored_job = SimpleNamespace(
        status="running", finished_at=None, error=None, output=None, job_metadata={}
    )
    session_factory = FakeSessionFactory(stored_job)
    service = AnalysisService(job_service, session_factory, loop)
    service._update_progress("job-progress", 1, 2, "halfway")
    assert stored_job.job_metadata["progress"]["message"] == "halfway"

    monkeypatch.setattr("aap_migration.api.crypto.decrypt_token", lambda token: f"dec:{token}")

    class FakeAnalyzer:
        def __init__(self, client, progress_callback):
            self.progress_callback = progress_callback

        async def analyze_all_organizations(self):
            self.progress_callback(1, 2, "halfway")
            return make_global_report()

    class FakeSourceClient:
        def __init__(self, config):
            self.config = config

    monkeypatch.setattr(
        "aap_migration.analysis.dependency_analyzer.CrossOrgDependencyAnalyzer", FakeAnalyzer
    )
    monkeypatch.setattr("aap_migration.client.aap_source_client.AAPSourceClient", FakeSourceClient)
    monkeypatch.setattr(
        "aap_migration.config.AAPInstanceConfig", lambda **kwargs: SimpleNamespace(**kwargs)
    )

    scheduled = {}

    def fake_run_coroutine_threadsafe(coro, _loop):
        task = asyncio.create_task(coro)
        scheduled["task"] = task
        return task

    monkeypatch.setattr(
        analysis_module.asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe
    )

    conn = SimpleNamespace(
        id="conn-1",
        url="https://source.example.com",
        token="abc",
        verify_ssl=False,
        api_prefix="/api/v2",
    )
    job_id = service.start_analysis(conn)
    await scheduled["task"]

    assert job_id in job_service.registered
    assert stored_job.status == "completed"
    assert stored_job.job_metadata["total_organizations"] == 2
    assert stored_job.job_metadata["quality_summary"]["average_quality_score"] == 91.5
    assert stored_job.job_metadata["circular_dependencies"] == [["OrgA", "OrgB"]]
    assert any("[1/2] halfway" in msg for _, msg in job_service.logs)

    adapter_responses = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_httpx_get(url, headers=None, params=None, verify=None, timeout=None):
        adapter_responses.append((url, headers or {}, params or {}, verify, timeout))
        if url.endswith("/api/v2/"):
            return FakeResponse({"inventories": "/api/v2/inventories/", "health": {"bad": True}})
        if params == {"page": 1, "page_size": 200}:
            return FakeResponse({"results": [{"id": 1}], "next": "page2"})
        if params == {"page": 2, "page_size": 200}:
            return FakeResponse({"results": [{"id": 2}], "next": None})
        if params == {"page": 1, "page_size": 25, "search": "foo"}:
            return FakeResponse({"count": 1, "results": [{"id": 10}]})
        raise RuntimeError("boom")

    monkeypatch.setattr(platform_module.httpx, "get", fake_httpx_get)
    platform_conn = SimpleNamespace(
        url="https://ctrl.example.com", token="tok", verify_ssl=True, type="awx", api_prefix=""
    )
    adapter = platform_module.PlatformAdapter(platform_conn)
    assert adapter.discover_resource_types() == [
        {"name": "inventories", "label": "Inventories", "api_path": "/api/v2/inventories/"}
    ]
    assert adapter.fetch_all("inventories") == [{"id": 1}, {"id": 2}]
    assert adapter.list_resources("inventories", 1, 25, "foo") == {
        "count": 1,
        "results": [{"id": 10}],
        "page": 1,
        "page_size": 25,
    }
    assert adapter.list_resources("inventories", 1, 25, "bar")["error"] == "boom"
    assert adapter_responses[0][1]["Authorization"] == "Bearer tok"

    awx_conn = SimpleNamespace(
        url="https://awx.example.com/", token="secret", verify_ssl=False, type="awx", api_prefix=""
    )
    controller_conn = SimpleNamespace(
        url="https://ctrl.example.com/",
        token="secret",
        verify_ssl=True,
        type="controller",
        api_prefix="",
    )
    awx_cfg = connection_to_aap_config(awx_conn)
    controller_cfg = connection_to_aap_config(controller_conn)
    migration_cfg = build_migration_config(awx_conn, controller_conn, "sqlite:///state.db")
    assert awx_cfg.url == "https://awx.example.com/api/v2"
    assert controller_cfg.url == "https://ctrl.example.com/api/controller/v2"
    assert migration_cfg.state.db_path == "sqlite:///state.db"


def make_global_report():
    dep = SimpleNamespace(
        resource_type="projects", resource_id=7, resource_name="Proj", required_by=["Job A"]
    )
    duplicate = SimpleNamespace(
        name="dup",
        resource_type="hosts",
        count=2,
        ids=[1, 2],
        severity="high",
        impact="conflict",
        recommendation="rename",
    )
    naming = SimpleNamespace(
        dominant_pattern="snake_case",
        consistency_score=98,
        total_resources=3,
        case_style="lower",
        prefixes=["pre"],
        separators=["_"],
        violations=["badName"],
    )
    org_a = SimpleNamespace(
        org_id=1,
        resource_count=4,
        has_cross_org_deps=True,
        can_migrate_standalone=False,
        required_migrations_before=["OrgB"],
        dependencies={"OrgB": [dep]},
        quality_report=SimpleNamespace(
            quality_score=90, duplicate_count=1, duplicates=[duplicate], naming_pattern=naming
        ),
        resources={"projects": [{"id": 1}], "hosts": [{"id": 2}, {"id": 3}]},
    )
    org_b = SimpleNamespace(
        org_id=2,
        resource_count=2,
        has_cross_org_deps=True,
        can_migrate_standalone=False,
        required_migrations_before=["OrgA"],
        dependencies={"OrgA": [dep]},
        quality_report=None,
        resources={"users": [{"id": 4}]},
    )

    class Report:
        analysis_date = datetime(2026, 5, 18, tzinfo=UTC)
        source_url = "https://source.example.com"
        total_organizations = 2
        analyzed_organizations = 2
        independent_orgs = []
        dependent_orgs = ["OrgA", "OrgB"]
        migration_order = ["OrgA", "OrgB"]
        migration_phases = [["OrgA"], ["OrgB"]]
        global_resources = {"execution_environments": [{"id": 1}]}
        total_duplicates = 1
        average_quality_score = 91.5
        org_reports = {"OrgA": org_a, "OrgB": org_b}

        def get_quality_summary(self):
            return {"average_quality_score": 91.5}

    return Report()


def test_serialize_report_handles_quality_summary_failures():
    report = make_global_report()

    class BrokenReport(type(report)):
        def get_quality_summary(self):
            raise RuntimeError("no summary")

    broken = BrokenReport()
    broken.__dict__.update(report.__dict__)
    serialized = _serialize_report(broken)
    assert serialized["quality_summary"] is None
    assert serialized["organizations"]["OrgA"]["blocks"] == ["OrgB"]
