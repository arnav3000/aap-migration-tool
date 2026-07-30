from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aap_migration.api.routers import migration, operations
from aap_migration.api.schemas import MigrationPreviewRequest, MigrationRunRequest


class FakeJobService:
    def __init__(self) -> None:
        self.started = []
        self.jobs = {}

    def start_job(self, name, job_type, callback):
        self.started.append((name, job_type, callback))
        return f"{job_type}-job"

    def get_job(self, job_id):
        return self.jobs.get(job_id)


class FakeJob:
    def __init__(self, status="completed", result=None) -> None:
        self.status = status
        self.result = result

    def to_dict(self):
        return {"status": self.status}


@pytest.mark.asyncio
async def test_migration_router_preview_run_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = FakeJobService()
    source = SimpleNamespace(id="src", name="Source", url="https://source.example.com")
    target = SimpleNamespace(id="dst", name="Target", url="https://target.example.com")
    monkeypatch.setattr(
        migration.ConnectionService,
        "get",
        lambda db, conn_id: {"src": source, "dst": target}.get(conn_id),
    )
    monkeypatch.setattr(
        migration.ConnectionService,
        "build_instance_config",
        lambda conn: SimpleNamespace(url=conn.url),
    )
    monkeypatch.setattr(migration.ConnectionService, "_auth_scheme", lambda conn: "Token")
    monkeypatch.setattr(migration, "get_job_service", lambda: svc)
    monkeypatch.setattr(migration, "get_exportable_types", lambda: ["organizations", "users"])

    class FakeSourceClient:
        def __init__(self, config, auth_scheme=None):
            self.url = config.url

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get_paginated(self, endpoint, page_size=200):
            if endpoint == "organizations/":
                return [{"id": 1, "name": "Default"}]
            if endpoint == "users/":
                return [
                    {"id": 2, "username": "alice", "organization": 1},
                    {"id": 3, "username": "bob", "organization": 2},
                ]
            return []

    class FakeTargetClient(FakeSourceClient):
        async def get_paginated(self, endpoint, page_size=200):
            if endpoint == "organizations/":
                return []
            if endpoint == "users/":
                return [{"id": 9, "username": "alice"}]
            return []

    monkeypatch.setattr("aap_migration.client.aap_source_client.AAPSourceClient", FakeSourceClient)
    monkeypatch.setattr("aap_migration.client.aap_target_client.AAPTargetClient", FakeTargetClient)

    preview_response = await migration.migration_preview(
        MigrationPreviewRequest(source_id="src", destination_id="dst", organizations=[1]),
        db=None,
    )
    assert preview_response.job_id == "preview-job"
    _, _, preview_callback = svc.started[0]
    logs = []
    preview_result = await preview_callback(FakeJob(), logs.append)
    assert preview_result["resources"]["organizations"][0]["action"] == "create"
    assert preview_result["resources"]["users"][0]["action"] == "skip"
    assert (
        "users" not in preview_result["resources"] or len(preview_result["resources"]["users"]) == 1
    )
    assert any("Filtering to organizations: [1]" in line for line in logs)

    svc.jobs["preview-job"] = FakeJob(status="completed", result={"hello": "world"})
    merged = migration.get_migration_preview("preview-job")
    assert merged["hello"] == "world"

    run_response = await migration.migration_run(
        MigrationRunRequest(
            source_id="src",
            destination_id="dst",
            job_id="preview-job",
            exclusions={"users": [2]},
            organizations=[1],
            name_prefix="pre-",
        ),
        db=None,
    )
    assert run_response.job_id == "migration-run-job"
    _, _, run_callback = svc.started[1]
    run_logs = []
    run_result = await run_callback(FakeJob(), run_logs.append)
    assert run_result == {"total_created": 1, "total_skipped": 0, "total_failed": 0}
    events = [json.loads(line[1:]) for line in run_logs if line.startswith("\t{")]
    assert any(event["_event"] == "migration_start" for event in events)
    assert all(event.get("resource_type") != "users" for event in events if "_event" in event)

    class DeleteQuery:
        def __init__(self, value):
            self.value = value

        def delete(self):
            return self.value

    class FakeDb:
        def __init__(self):
            self.committed = False

        def query(self, model):
            name = getattr(model, "__name__", "")
            return DeleteQuery(2 if name == "MigrationProgress" else 3)

        def commit(self):
            self.committed = True

    clear_result = migration.clear_migration_state(db=FakeDb())
    assert clear_result.cleared_progress == 2
    assert clear_result.deleted_mappings == 3
    assert migration.get_exclusions() == {"migration": {}, "cleanup": {}}


@pytest.mark.asyncio
async def test_operations_router_cleanup_and_export(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = FakeJobService()
    conn = SimpleNamespace(id="conn-1", name="Target", url="https://target.example.com")
    monkeypatch.setattr(
        operations.ConnectionService,
        "get",
        lambda db, conn_id: conn if conn_id == "conn-1" else None,
    )
    monkeypatch.setattr(operations, "get_job_service", lambda: svc)

    class FakeTargetClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def list_resources(self, resource_type):
            if resource_type == "organizations":
                return [{"id": 1}, {"id": 2}]
            return []

        async def delete_resource(self, resource_type, resource_id):
            return None

    class FakeSourceClient(FakeTargetClient):
        async def get_paginated(self, endpoint, page_size=200):
            if endpoint == "organizations/":
                return [{"id": 1}, {"id": 2}]
            return []

    monkeypatch.setattr(
        operations.ConnectionService, "build_target_client", lambda conn: FakeTargetClient()
    )
    monkeypatch.setattr(
        operations.ConnectionService, "build_source_client", lambda conn: FakeSourceClient()
    )
    monkeypatch.setattr(
        "aap_migration.resources.get_cleanup_order", lambda: ["organizations", "settings"]
    )
    monkeypatch.setattr(
        "aap_migration.resources.get_exportable_types", lambda: ["organizations", "teams"]
    )

    state_session = SimpleNamespace(
        statements=[],
        committed=False,
        closed=False,
        execute=lambda sql, params: state_session.statements.append(params["rt"]),
        commit=lambda: setattr(state_session, "committed", True),
        close=lambda: setattr(state_session, "closed", True),
        rollback=lambda: None,
    )
    app_state = SimpleNamespace(db_session_factory=lambda: state_session)
    monkeypatch.setattr("aap_migration.api.dependencies.get_app_state", lambda: app_state)

    cleanup_response = await operations.run_cleanup("conn-1", db=None)
    assert cleanup_response.job_id == "cleanup-job"
    _, _, cleanup_callback = svc.started[0]
    cleanup_logs = []
    cleanup_result = await cleanup_callback(FakeJob(), cleanup_logs.append)
    assert cleanup_result == {"deleted": 2, "errors": 0}
    assert state_session.statements == ["organizations", "organizations"]
    assert any("Clearing migration state" in line for line in cleanup_logs)

    export_response = await operations.run_export("conn-1", db=None)
    assert export_response.job_id == "export-job"
    _, _, export_callback = svc.started[1]
    export_logs = []
    export_result = await export_callback(FakeJob(), export_logs.append)
    assert export_result["status"] == "completed"
    assert export_result["exported"]["organizations"] == 2
    assert any("Export complete" in line for line in export_logs)
