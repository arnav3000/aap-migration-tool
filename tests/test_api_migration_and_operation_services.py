from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import aap_migration.api.models as api_models
import aap_migration.api.schemas as api_schemas


class DummyConn:
    pass


class DummyJobModel:
    id = "id"


api_models.Job = DummyJobModel
api_schemas.MigrationPreviewResponse = type("MigrationPreviewResponse", (), {})

from aap_migration.api.services.migration_service import (  # noqa: E402
    JobLogHandler,
    MigrationService,
)
from aap_migration.api.services.operation_service import OperationService  # noqa: E402


class FakeJobService:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []
        self.completed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.tasks: dict[str, object] = {}
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


class FakeQuery:
    def __init__(self, result) -> None:
        self.result = result

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.result


class FakeSession:
    def __init__(self, result=None) -> None:
        self.result = result

    def query(self, model):
        return FakeQuery(self.result)

    def close(self) -> None:
        pass


class FakeSessionFactory:
    def __init__(self, result=None) -> None:
        self.result = result

    def __call__(self):
        return FakeSession(self.result)


@pytest.mark.asyncio
async def test_job_log_handler_formats_progress_and_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_service = FakeJobService()
    handler = JobLogHandler(job_service, "job-1")

    current_time = {"value": 100.0}
    monkeypatch.setattr(
        "aap_migration.api.services.migration_service.time.time", lambda: current_time["value"]
    )

    def emit(message: str, level: int = logging.INFO) -> None:
        record = logging.LogRecord("test", level, __file__, 1, message, (), None)
        handler.emit(record)

    emit("migration_started total_phases=2")
    emit("phase_starting description=Organizations")
    current_time["value"] = 105.0
    emit("export_completed total_exported=12")
    emit("resource_created")
    emit("resource_skipped skipped_count=2")
    emit("resource_import_failed source_name=proj-a error=some failure happened")
    emit("phase_completed")
    emit("phase_starting description=Users")
    emit("api_request noisy")
    emit("something bad version=2.6.0", level=logging.WARNING)
    emit("migration_completed")

    output = "\n".join(msg for _, msg in job_service.logs)
    assert "Migration started (2 phases)" in output
    assert "[1/2] Organizations" in output
    assert "Exported 12 resources" in output
    assert "OK:1 Skip:2 Err:1" in output
    assert "Failed: proj-a" in output
    assert "Done: 1 created, 2 skipped, 1 failed" in output
    assert "Migration complete: 2 created, 4 skipped, 2 failed" in output
    assert "something bad" in output
    assert "api_request noisy" not in output


@pytest.mark.asyncio
async def test_migration_service_preview_and_run_paths(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aap_migration.api.services.migration_service as migration_module

    job_service = FakeJobService()
    preview_job = SimpleNamespace(
        job_metadata={
            "resources": {
                "projects": [{"source_id": 1}, {"source_id": 2}],
                "users": [{"source_id": 5}, {"source_id": 6}],
            }
        }
    )
    session_factory = FakeSessionFactory(preview_job)
    loop = asyncio.get_running_loop()
    service = MigrationService(job_service, session_factory, loop)

    monkeypatch.setattr(migration_module, "Connection", DummyConn)
    monkeypatch.setattr(api_models, "Connection", DummyConn)
    monkeypatch.setattr(
        service, "_create_job", lambda job_type, connection_id=None: f"{job_type}-job"
    )
    finished = []
    monkeypatch.setattr(
        service,
        "_finish_job",
        lambda job_id, status, error=None, metadata=None: finished.append(
            (job_id, status, error, metadata)
        ),
    )
    monkeypatch.setattr(service, "_get_db_url", lambda: str(tmp_path / "state.db"))

    class FakePlatformAdapter:
        def __init__(self, conn):
            self.conn = conn

        def fetch_all(self, resource_type: str):
            source = {
                "organizations": [{"id": 1, "name": "Default"}],
                "inventories": [{"id": 2, "name": "InvA", "total_hosts": 3, "total_groups": 1}],
                "users": [{"id": 5, "username": "alice"}],
            }
            dest = {
                "organizations": [{"id": 9, "name": "Default"}],
                "inventories": [],
                "users": [{"id": 7, "username": "bob"}],
            }
            return (
                source.get(resource_type, [])
                if self.conn.name == "Source"
                else dest.get(resource_type, [])
            )

    monkeypatch.setattr(
        "aap_migration.api.services.platform_adapter.PlatformAdapter",
        FakePlatformAdapter,
    )

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

    preview_job_id = service.start_preview(source, dest)
    await job_service.tasks[preview_job_id]

    assert preview_job_id == "migration-preview-job"
    assert job_service.completed == ["migration-preview-job"]
    assert finished[0][1] == "completed"
    assert finished[0][3]["host_counts"] == {"InvA": 3}
    assert finished[0][3]["group_counts"] == {"InvA": 1}
    preview_logs = "\n".join(msg for jid, msg in job_service.logs if jid == preview_job_id)
    assert "Starting migration preview: Source -> Destination" in preview_logs
    assert "Preview complete: 2 to create, 1 to skip" in preview_logs

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

    state_holder = {}

    class FakeCoordinator:
        def __init__(
            self, config, source_client, target_client, state, enable_progress, show_stats
        ):
            state_holder["state"] = state

        async def migrate_all(self, skip_phases=None, generate_report=True, report_dir="./reports"):
            migrate_calls.append((skip_phases, generate_report, report_dir))
            return {
                "status": "completed",
                "total_resources_exported": 9,
                "total_resources_imported": 7,
                "total_resources_failed": 1,
                "total_resources_skipped": 2,
            }

    monkeypatch.setattr(
        "aap_migration.api.services.engine_adapter.build_migration_config",
        lambda src, dst, db_url: SimpleNamespace(source="src", target="dst", state="state"),
    )
    monkeypatch.setattr("aap_migration.client.aap_source_client.AAPSourceClient", FakeSourceClient)
    monkeypatch.setattr("aap_migration.client.aap_target_client.AAPTargetClient", FakeTargetClient)
    monkeypatch.setattr("aap_migration.migration.state.MigrationState", FakeState)
    monkeypatch.setattr("aap_migration.migration.coordinator.MigrationCoordinator", FakeCoordinator)

    run_job_id = service.start_run(
        source,
        dest,
        "preview-job",
        exclusions={"projects": [1, 2], "users": [5]},
    )
    await job_service.tasks[run_job_id]

    assert run_job_id == "migration-run-job"
    assert migrate_calls == [(["projects"], True, "./reports")]
    assert state_holder["state"].created == [("users", 5, "excluded-5")]
    assert state_holder["state"].skipped == [("users", 5, "Excluded by user in migration preview")]
    run_logs = "\n".join(msg for jid, msg in job_service.logs if jid == run_job_id)
    assert "Skipping entire phase: projects" in run_logs
    assert "Excluded 1 users resource(s) from migration" in run_logs
    assert "Migration completed: exported=9 imported=7 failed=1 skipped=2" in run_logs


@pytest.mark.asyncio
async def test_migration_service_failure_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    import aap_migration.api.services.migration_service as migration_module

    job_service = FakeJobService()
    session_factory = FakeSessionFactory()
    loop = asyncio.get_running_loop()
    service = MigrationService(job_service, session_factory, loop)

    monkeypatch.setattr(migration_module, "Connection", DummyConn)
    monkeypatch.setattr(api_models, "Connection", DummyConn)
    monkeypatch.setattr(
        service, "_create_job", lambda job_type, connection_id=None: f"{job_type}-job"
    )
    finished = []
    monkeypatch.setattr(
        service,
        "_finish_job",
        lambda job_id, status, error=None, metadata=None: finished.append((job_id, status, error)),
    )
    monkeypatch.setattr(
        "aap_migration.api.services.platform_adapter.PlatformAdapter",
        lambda conn: (_ for _ in ()).throw(RuntimeError("preview boom")),
    )

    source = SimpleNamespace(
        id="src",
        name="Source",
        url="https://s",
        token="",
        verify_ssl=True,
        type="awx",
        api_prefix="",
    )
    dest = SimpleNamespace(
        id="dst", name="Dest", url="https://d", token="", verify_ssl=True, type="awx", api_prefix=""
    )

    preview_job_id = service.start_preview(source, dest)
    await job_service.tasks[preview_job_id]
    assert job_service.failed == [("migration-preview-job", "preview boom")]
    assert finished[0] == ("migration-preview-job", "failed", "preview boom")

    job_service.failed.clear()
    finished.clear()
    monkeypatch.setattr(
        "aap_migration.api.services.engine_adapter.build_migration_config",
        lambda src, dst, db_url: (_ for _ in ()).throw(RuntimeError("run boom")),
    )
    run_job_id = service.start_run(source, dest, "preview-job")
    await job_service.tasks[run_job_id]
    assert job_service.failed == [("migration-run-job", "run boom")]
    assert finished[0] == ("migration-run-job", "failed", "run boom")


@pytest.mark.asyncio
async def test_operation_service_cleanup_and_export(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aap_migration.api.services.operation_service as operation_module

    job_service = FakeJobService()
    service = OperationService(job_service, FakeSessionFactory(), asyncio.get_running_loop())

    monkeypatch.setattr(operation_module, "Connection", DummyConn)
    monkeypatch.setattr(service, "_create_job", lambda job_type, connection_id: f"{job_type}-job")
    finished = []
    monkeypatch.setattr(
        service,
        "_finish_job",
        lambda job_id, status, error=None: finished.append((job_id, status, error)),
    )
    monkeypatch.setattr(service, "_get_db_url", lambda: str(tmp_path / "state.db"))

    class DummyTargetClient:
        def __init__(self, config):
            self.config = config

    async def fake_cancel_all_jobs(client, config):
        return {"cancelled": 2}

    async def fake_delete_resources(client, resource_type, config, skip_default=True):
        if resource_type == "hosts":
            raise RuntimeError("host failure")
        if resource_type == "projects":
            return (2, 1, 0, [])
        return (1, 0, 0, [])

    import aap_migration.cli.commands.cleanup as cleanup_module

    monkeypatch.setattr(
        "aap_migration.api.services.engine_adapter.connection_to_aap_config",
        lambda conn: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "aap_migration.config.AAPInstanceConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        "aap_migration.config.StateConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        "aap_migration.config.MigrationConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr("aap_migration.client.aap_target_client.AAPTargetClient", DummyTargetClient)
    monkeypatch.setattr(cleanup_module, "cancel_all_jobs", fake_cancel_all_jobs)
    monkeypatch.setattr(cleanup_module, "delete_resources", fake_delete_resources)

    conn = SimpleNamespace(
        id="conn-1",
        name="Target Env",
        url="https://target.example.com",
        token="",
        verify_ssl=True,
        type="awx",
        api_prefix="",
    )

    cleanup_job_id = service.start_cleanup(conn)
    await job_service.tasks[cleanup_job_id]
    cleanup_logs = "\n".join(msg for jid, msg in job_service.logs if jid == cleanup_job_id)
    assert "Starting cleanup on Target Env" in cleanup_logs
    assert "Cancelled jobs" in cleanup_logs
    assert "hosts: error - host failure" in cleanup_logs
    assert "Cleanup complete: deleted=" in cleanup_logs
    assert finished[0][1] == "completed"

    monkeypatch.chdir(tmp_path)

    class FakePlatformAdapter:
        def __init__(self, conn):
            self.conn = conn

        def fetch_all(self, resource_type: str):
            if resource_type == "organizations":
                return [{"id": 1, "name": "Default"}]
            if resource_type == "credentials":
                raise RuntimeError("cred export failed")
            return []

    monkeypatch.setattr(
        "aap_migration.api.services.platform_adapter.PlatformAdapter",
        FakePlatformAdapter,
    )

    export_job_id = service.start_export(conn)
    await job_service.tasks[export_job_id]
    export_logs = "\n".join(msg for jid, msg in job_service.logs if jid == export_job_id)
    assert "Starting export from Target Env" in export_logs
    assert "Exported 1 organizations" in export_logs
    assert "Error exporting credentials: cred export failed" in export_logs
    assert "Export complete: 1 resources" in export_logs
    assert (
        tmp_path / "exports" / "Target_Env" / "organizations" / "organizations_001.json"
    ).exists()
