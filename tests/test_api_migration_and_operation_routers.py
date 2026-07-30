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
async def test_migration_router_preview_run_and_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
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
        lambda conn: SimpleNamespace(
            url=conn.url,
            token="tok",
            verify_ssl=True,
            timeout=30,
        ),
    )
    monkeypatch.setattr(migration.ConnectionService, "_auth_scheme", lambda conn: "Token")
    monkeypatch.setattr(migration, "get_job_service", lambda: svc)
    monkeypatch.setattr(migration, "get_db_url", lambda: f"sqlite:///{tmp_path / 'preview.db'}")
    monkeypatch.setattr(
        "aap_migration.resources.get_fully_supported_types",
        lambda: ["organizations", "users"],
    )

    async def _async_zero(*_a, **_k):
        return 0

    monkeypatch.setattr(
        "aap_migration.migration.credential_type_utils.map_managed_credential_types",
        _async_zero,
    )

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
                    {"id": 2, "username": "alice"},
                    {"id": 3, "username": "bob"},
                ]
            return []

        async def list_resources(self, resource_type, page_size=200):
            endpoint = {"organizations": "organizations/", "users": "users/"}.get(
                resource_type, f"{resource_type}/"
            )
            return await self.get_paginated(endpoint, page_size=page_size)

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
    assert preview_result["resources"]["users"][0]["name"] == "alice"
    assert preview_result["bootstrap"]["mapped"] >= 1
    assert any("Filtering to organizations: [1]" in line for line in logs)
    assert any("Scanning source and target" in line for line in logs)

    svc.jobs["preview-job"] = FakeJob(status="completed", result={"hello": "world"})
    merged = migration.get_migration_preview("preview-job")
    assert merged["hello"] == "world"

    captured: dict[str, object] = {}

    def fake_build_source_contexts(source_configs, dest_cfg, dest_auth_scheme, db_url):
        captured["source_configs"] = source_configs
        sources = [
            {
                **cfg,
                "src_client": object(),
                "state": object(),
                "migration_config": SimpleNamespace(performance=None, resource_mappings={}),
            }
            for cfg in source_configs
        ]
        return dest_cfg, object(), sources

    async def fake_migrate_resource_type(
        rtype, sources, target_client, phase_num, emit, log, created_creds
    ):
        captured.setdefault("migrated_types", []).append(rtype)  # type: ignore[union-attr]
        assert sources[0].get("excluded_ids") == {"users": [2]}
        assert sources[0].get("name_prefix") == "pre-"
        emit(
            {
                "_event": "resource_result",
                "phase_num": phase_num,
                "name": "pre-Default" if rtype == "organizations" else "alice",
                "resource_type": rtype,
                "result": "created" if rtype == "organizations" else "skipped",
                "detail": "" if rtype == "organizations" else "Excluded by user",
            }
        )
        if rtype == "organizations":
            return 1, 0, 0, 1
        return 0, 1, 0, 0

    async def fake_run_cac_org_update(sources, target_client, phase_num, emit, log):
        return 0

    monkeypatch.setattr(
        "aap_migration.api.routers.planner._build_source_contexts",
        fake_build_source_contexts,
    )
    monkeypatch.setattr(
        "aap_migration.api.routers.planner._migrate_resource_type",
        fake_migrate_resource_type,
    )
    monkeypatch.setattr(
        "aap_migration.api.routers.planner._run_cac_org_update",
        fake_run_cac_org_update,
    )

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
    assert run_result == {
        "total_created": 1,
        "total_skipped": 1,
        "total_failed": 0,
        "total_updated": 0,
    }
    assert captured["migrated_types"] == ["organizations", "users"]
    assert captured["source_configs"][0]["name_prefix"] == "pre-"  # type: ignore[index]
    assert captured["source_configs"][0]["org_ids"] == [1]  # type: ignore[index]
    events = [json.loads(line[1:]) for line in run_logs if line.startswith("\t{")]
    assert any(event["_event"] == "migration_start" for event in events)
    assert any(
        event.get("resource_type") == "organizations" and event.get("result") == "created"
        for event in events
    )
    assert any("Applying name prefix: 'pre-'" in line for line in run_logs)

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
async def test_operations_cleanup_returns_404_for_missing_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    monkeypatch.setattr(
        operations.ConnectionService,
        "get",
        lambda db, conn_id: None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await operations.run_cleanup("missing", db=None)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_operations_export_returns_404_for_missing_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    monkeypatch.setattr(
        operations.ConnectionService,
        "get",
        lambda db, conn_id: None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await operations.run_export("missing", db=None)

    assert exc_info.value.status_code == 404


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


@pytest.mark.asyncio
async def test_resolve_jt_dependencies_includes_inventory_sources() -> None:
    logs: list[str] = []

    class FakeClient:
        async def get_resource_by_id(self, resource_type: str, resource_id: int) -> dict:
            if resource_type == "job_templates":
                return {
                    "id": 1,
                    "name": "Demo JT",
                    "inventory": 10,
                    "project": 20,
                    "organization": 1,
                }
            if resource_type == "projects":
                return {"id": 20, "organization": 1}
            if resource_type == "inventories":
                return {"id": 10, "organization": 1}
            raise AssertionError(f"unexpected fetch {resource_type}/{resource_id}")

        async def get_job_template_credentials(self, job_template_id: int) -> list:
            return []

        async def get_inventory_sources(self, params: dict | None = None) -> list:
            if params and params.get("inventory") == 10:
                return [
                    {
                        "id": 100,
                        "name": "scm-source",
                        "inventory": 10,
                        "source_project": 20,
                        "credential": 30,
                    }
                ]
            return []

    deps, jt_data = await operations._resolve_jt_dependencies(FakeClient(), [1], logs.append)

    assert len(jt_data) == 1
    assert deps["inventory_sources"] == {100}
    assert deps["inventories"] == {10}
    assert deps["projects"] == {20}


@pytest.mark.asyncio
async def test_resolve_jt_dependencies_inventory_sources_nested_fallback() -> None:
    logs: list[str] = []

    class FakeClient:
        async def get_resource_by_id(self, resource_type: str, resource_id: int) -> dict:
            if resource_type == "job_templates":
                return {"id": 1, "inventory": 10, "project": 20, "organization": 1}
            if resource_type == "projects":
                return {"id": 20, "organization": 1}
            if resource_type == "inventories":
                return {"id": 10, "organization": 1}
            raise AssertionError(f"unexpected fetch {resource_type}/{resource_id}")

        async def get_job_template_credentials(self, job_template_id: int) -> list:
            return []

        async def get_inventory_sources(self, params: dict | None = None) -> list:
            return []

        async def get_paginated(self, endpoint: str, **kwargs) -> list:
            if endpoint == "inventories/10/inventory_sources/":
                return [{"id": 100, "inventory": 10, "source_project": 20}]
            return []

    deps, _ = await operations._resolve_jt_dependencies(FakeClient(), [1], logs.append)

    assert deps["inventory_sources"] == {100}
    assert any("nested endpoint" in line for line in logs)


@pytest.mark.asyncio
async def test_should_skip_migrated_resource_reimports_when_target_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs: list[str] = []
    cleared: list[tuple[str, int]] = []

    monkeypatch.setattr(
        operations,
        "_clear_selective_resource_state",
        lambda state, rt, sid: cleared.append((rt, sid)),
    )

    class FakeState:
        def is_migrated(self, resource_type: str, source_id: int) -> bool:
            return resource_type == "inventory_sources" and source_id == 100

        def get_mapped_id(self, resource_type: str, source_id: int) -> int | None:
            return 999

    class MissingTargetClient:
        async def get(self, endpoint: str) -> dict:
            raise RuntimeError("not found")

    should_skip = await operations._should_skip_migrated_resource(
        FakeState(),
        MissingTargetClient(),
        "inventory_sources",
        100,
        logs.append,
    )

    assert should_skip is False
    assert cleared == [("inventory_sources", 100)]
    assert any("re-importing" in line for line in logs)


@pytest.mark.asyncio
async def test_should_skip_migrated_resource_when_target_exists() -> None:
    class FakeState:
        def is_migrated(self, resource_type: str, source_id: int) -> bool:
            return True

        def get_mapped_id(self, resource_type: str, source_id: int) -> int | None:
            return 42

    class TargetClient:
        async def get(self, endpoint: str) -> dict:
            return {"id": 42}

    should_skip = await operations._should_skip_migrated_resource(
        FakeState(),
        TargetClient(),
        "inventory_sources",
        100,
        lambda _msg: None,
    )

    assert should_skip is True


@pytest.mark.asyncio
async def test_selective_migrate_returns_404_when_connection_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from aap_migration.api.schemas import SelectiveMigrateRequest

    monkeypatch.setattr(
        operations.ConnectionService,
        "get",
        lambda db, conn_id: None,
    )

    body = SelectiveMigrateRequest(
        source_id="missing-src",
        destination_id="missing-dst",
        job_template_ids=[1],
    )

    with pytest.raises(HTTPException) as exc_info:
        await operations.selective_migrate(body, db=None)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_selective_migrate_returns_404_when_destination_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from aap_migration.api.schemas import SelectiveMigrateRequest

    source = SimpleNamespace(id="src-1", name="Source", url="https://source.example.com")
    monkeypatch.setattr(
        operations.ConnectionService,
        "get",
        lambda db, conn_id: source if conn_id == "src-1" else None,
    )

    body = SelectiveMigrateRequest(
        source_id="src-1",
        destination_id="dst-missing",
        job_template_ids=[1],
    )

    with pytest.raises(HTTPException) as exc_info:
        await operations.selective_migrate(body, db=None)

    assert exc_info.value.status_code == 404
    assert "Destination" in exc_info.value.detail


@pytest.mark.asyncio
async def test_selective_migrate_starts_background_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aap_migration.api.schemas import SelectiveMigrateRequest

    svc = FakeJobService()
    source = SimpleNamespace(id="src-1", name="Source", url="https://source.example.com")
    dest = SimpleNamespace(id="dst-1", name="Dest", url="https://dest.example.com")

    monkeypatch.setattr(
        operations.ConnectionService,
        "get",
        lambda db, conn_id: source if conn_id == "src-1" else dest,
    )
    monkeypatch.setattr(operations, "get_job_service", lambda: svc)
    monkeypatch.setattr(operations, "get_db_url", lambda: "sqlite:///:memory:")
    monkeypatch.setattr(
        operations.ConnectionService,
        "build_instance_config",
        lambda conn: SimpleNamespace(url=conn.url, token="token"),
    )
    monkeypatch.setattr(
        operations.ConnectionService,
        "_auth_scheme",
        lambda conn: "Bearer",
    )

    body = SelectiveMigrateRequest(
        source_id="src-1",
        destination_id="dst-1",
        job_template_ids=[1],
    )
    response = await operations.selective_migrate(body, db=None)
    assert response.job_id == "selective-migration-job"
    assert len(svc.started) == 1
    assert "Selective migrate" in svc.started[0][0]
