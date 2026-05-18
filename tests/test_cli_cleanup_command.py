from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aap_migration.cli.commands import cleanup as cleanup_module
from aap_migration.client.exceptions import (
    APIError,
    PendingDeletionError,
    ResourceInUseError,
)
from aap_migration.migration.models import IDMapping, MigrationProgress


def _unwrap_callback(command) -> object:
    callback = command.callback
    while hasattr(callback, "__wrapped__"):
        callback = callback.__wrapped__
    return callback


@pytest.mark.asyncio
async def test_cleanup_discovery_and_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeClient:
        async def get(self, endpoint, params=None):
            return {
                "organizations": "/api/v2/organizations/",
                "jobs": "/api/v2/jobs/",
                "description": "metadata",
            }

    discovered = await cleanup_module.discover_target_resources(FakeClient())
    assert discovered == ["organizations", "jobs"]

    monkeypatch.setattr(cleanup_module, "NON_DELETABLE_RESOURCES", ["labels"])
    monkeypatch.setattr(
        "aap_migration.resources.READ_ONLY_ENDPOINTS",
        {"ping"},
    )
    monkeypatch.setattr(
        "aap_migration.resources.RUNTIME_DATA_ENDPOINTS",
        {"jobs"},
    )
    monkeypatch.setattr(
        "aap_migration.resources.MANUAL_MIGRATION_ENDPOINTS",
        {"settings"},
    )
    filtered = cleanup_module.filter_cleanup_resources(
        ["ping", "jobs", "labels", "settings", "organizations", "custom"]
    )
    assert filtered == ["organizations", "custom"]

    monkeypatch.setattr(
        "aap_migration.resources.RESOURCE_REGISTRY",
        {
            "organizations": SimpleNamespace(cleanup_order=5),
            "inventory": SimpleNamespace(cleanup_order=2),
        },
    )
    monkeypatch.setattr("aap_migration.resources.normalize_resource_type", lambda rtype: rtype)
    ordered = cleanup_module.sort_by_cleanup_order(["organizations", "inventory", "unknown"])
    assert ordered == ["inventory", "organizations", "unknown"]

    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir()
    (schemas_dir / "target_endpoints.json").write_text(
        json.dumps({"endpoints": {"organizations": {}, "jobs": {}, "labels": {}}})
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cleanup_module, "filter_cleanup_resources", lambda types: ["organizations"])
    monkeypatch.setattr(cleanup_module, "sort_by_cleanup_order", lambda types: ["organizations"])

    from_file = await cleanup_module.get_cleanup_resource_types(FakeClient(), use_discovered=True)
    assert from_file == ["organizations"]

    async def boom_discover(client):
        raise RuntimeError("no discovery")

    monkeypatch.setattr(cleanup_module, "discover_target_resources", boom_discover)
    monkeypatch.setattr(cleanup_module, "CLEANUP_ORDER", ["fallback"])
    (schemas_dir / "target_endpoints.json").unlink()
    fallback = await cleanup_module.get_cleanup_resource_types(FakeClient(), use_discovered=True)
    assert fallback == ["fallback"]


@pytest.mark.asyncio
async def test_cleanup_fetch_helpers_and_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        async def get(self, endpoint, params=None):
            if endpoint == "projects/":
                page = params.get("page", 1)
                if page == 1:
                    return {"count": 3, "results": [{"id": 1}], "next": True}
                if page == 2:
                    return {"count": 3, "results": [{"id": 2}], "next": True}
                return {"count": 3, "results": [{"id": 3}], "next": None}
            if endpoint == "users/":
                return {"count": 5}
            if endpoint == "teams/":
                raise RuntimeError("bad count")
            return {"results": []}

    config = SimpleNamespace(
        performance=SimpleNamespace(
            cleanup_page_fetch_concurrency=2,
            default_page_size=1,
            gateway_error_retry_attempts=1,
            gateway_error_backoff_base=0,
        )
    )
    monkeypatch.setattr(cleanup_module, "retry_on_gateway_error", lambda **kwargs: lambda fn: fn)
    resources = await cleanup_module.fetch_all_resources_parallel(FakeClient(), "projects/", config)
    assert [item["id"] for item in resources] == [1, 2, 3]

    monkeypatch.setattr(cleanup_module, "get_endpoint", lambda rtype: f"{rtype}/")
    counts = await cleanup_module.fetch_counts_parallel(FakeClient(), ["users", "teams"])
    assert counts == {"users": 5, "teams": 0}
    assert (
        cleanup_module.is_method_not_allowed_error(RuntimeError("405 method not allowed")) is True
    )

    running = {"count": 1}

    class WaitClient:
        async def get(self, endpoint, params=None):
            current = running["count"]
            running["count"] = 0
            return {"results": [{}] * current}

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(cleanup_module.asyncio, "sleep", fake_sleep)
    finished, still_running = await cleanup_module.wait_for_jobs_to_finish(
        WaitClient(),
        "jobs",
        expected_count=1,
        timeout=5,
        poll_interval=0,
    )
    assert (finished, still_running) == (1, 0)


@pytest.mark.asyncio
async def test_cleanup_job_control_and_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(
        performance=SimpleNamespace(
            default_page_size=50,
            cleanup_job_cancel_concurrency=3,
            cleanup_job_finish_timeout=10,
            cleanup_job_poll_interval=0,
            cleanup_max_concurrent=3,
        )
    )

    class FakeClient:
        def __init__(self) -> None:
            self.cancelled = []
            self.deleted = []

        async def get(self, endpoint, params=None):
            if endpoint in {
                "jobs/",
                "workflow_jobs/",
                "project_updates/",
                "inventory_updates/",
                "system_jobs/",
            }:
                if endpoint == "jobs/":
                    return {
                        "results": [{"id": 1, "name": "Job 1", "status": "running"}],
                        "next": None,
                    }
                if endpoint == "workflow_jobs/":
                    return {
                        "results": [{"id": 2, "name": "WF", "status": "successful"}],
                        "next": None,
                    }
                if endpoint == "project_updates/":
                    return {"results": [{"id": 3, "name": "PU", "status": "running"}], "next": None}
                return {"results": [], "next": None}
            return {"results": []}

        async def cancel_job(self, job_id, endpoint_prefix=None):
            if job_id == 3:
                raise APIError("no cancel", status_code=405)
            self.cancelled.append((job_id, endpoint_prefix))

        async def delete(self, endpoint):
            if endpoint.endswith("/2/"):
                raise RuntimeError("404 gone")
            self.deleted.append(endpoint)

    client = FakeClient()
    monkeypatch.setattr(
        cleanup_module, "wait_for_jobs_to_finish", lambda **kwargs: asyncio.sleep(0, result=(1, 0))
    )
    monkeypatch.setattr(
        cleanup_module,
        "delete_active_jobs",
        lambda client, endpoint, jobs, config: asyncio.sleep(0, result=(len(jobs), 0)),
    )
    cancelled = await cleanup_module.cancel_all_jobs(client, config)
    assert cancelled == (1, 0, 0, 0, 0)

    deleted, failed = await cleanup_module.delete_active_jobs(
        client,
        "jobs",
        [{"id": 1}, {"id": 2}],
        config,
    )
    assert (deleted, failed) == (2, 0)

    query_state = {"jobs/": 1}

    class EnsureClient:
        async def get(self, endpoint, params=None):
            if params and "status__in" in params:
                current = query_state.get(endpoint, 0)
                query_state[endpoint] = 0
                if params.get("page_size") == 1:
                    return {"count": current}
                return {
                    "results": [{"id": 9, "status": "running"}] if current else [],
                    "next": None,
                }
            return {"results": [], "next": None}

        async def cancel_job(self, job_id, endpoint_prefix=None):
            return None

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(cleanup_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        "aap_migration.resources.JOB_DELETABLE_TYPES",
        ["jobs"],
    )
    summary = await cleanup_module.ensure_no_active_jobs(
        EnsureClient(),
        config,
        cancel_timeout=1,
        delete_if_stuck=False,
    )
    assert summary["total_active"] == 1
    assert summary["total_cancelled"] == 1


@pytest.mark.asyncio
async def test_cleanup_resource_deletion_helpers(
    tmp_path: Path, db_session, sqlite_db_url: str, monkeypatch
) -> None:
    class RetryClient:
        def __init__(self) -> None:
            self.calls = 0

        async def delete_resource(self, endpoint, resource_id):
            self.calls += 1
            if self.calls == 1:
                raise ResourceInUseError("blocked", active_jobs=[{"id": 5}])
            return None

    retry_client = RetryClient()

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(cleanup_module.asyncio, "sleep", fake_sleep)
    await cleanup_module.delete_resource_with_retry(
        retry_client, "projects", 1, "Proj", max_retries=1, retry_delay=0
    )
    assert retry_client.calls == 2

    class ParallelClient:
        async def delete_resource(self, endpoint, resource_id):
            if resource_id == 2:
                raise PendingDeletionError("pending")
            if resource_id == 3:
                raise ResourceInUseError("blocked")
            if resource_id == 4:
                raise RuntimeError("boom")
            return None

    progress = []
    deleted, skipped, errors, failed = await cleanup_module.delete_resources_parallel(
        ParallelClient(),
        "projects",
        [
            {"id": 1, "name": "One"},
            {"id": 2, "name": "Two"},
            {"id": 3, "name": "Three"},
            {"id": 4, "name": "Four"},
        ],
        SimpleNamespace(performance=SimpleNamespace(cleanup_max_concurrent=2)),
        progress_callback=lambda d, s, e: progress.append((d, s, e)),
    )
    assert (deleted, skipped, errors) == (1, 1, 2)
    assert len(failed) == 2
    assert progress

    monkeypatch.setattr(cleanup_module, "NON_DELETABLE_RESOURCES", ["labels"])
    assert await cleanup_module.delete_resources(
        ParallelClient(),
        "labels",
        SimpleNamespace(
            performance=SimpleNamespace(cleanup_max_concurrent=2, host_cleanup_batch_size=100)
        ),
    ) == (0, 0, 0, [])

    async def fake_fetch_all_resources_parallel(client, endpoint, config):
        return [
            {"id": 1, "name": "Default"},
            {"id": 2, "name": "Keep", "managed": True},
            {"id": 3, "name": "Delete Me"},
        ]

    async def fake_delete_resources_parallel(
        client, endpoint, resources, config, progress_callback=None
    ):
        return (1, 0, 0, [])

    monkeypatch.setattr(
        cleanup_module, "fetch_all_resources_parallel", fake_fetch_all_resources_parallel
    )
    monkeypatch.setattr(cleanup_module, "delete_resources_parallel", fake_delete_resources_parallel)
    monkeypatch.setattr(cleanup_module, "get_endpoint", lambda rtype: f"{rtype}/")
    result = await cleanup_module.delete_resources(
        ParallelClient(),
        "organizations",
        SimpleNamespace(
            performance=SimpleNamespace(cleanup_max_concurrent=2, host_cleanup_batch_size=100)
        ),
        skip_default=True,
    )
    assert result == (1, 1, 0, [])

    progress_record = MigrationProgress(
        resource_type="projects",
        source_id=1,
        source_name="Proj",
        status="completed",
        phase="import",
    )
    db_session.add(progress_record)
    db_session.flush()
    db_session.add(
        IDMapping(
            resource_type="projects",
            source_id=1,
            target_id=10,
            migration_progress_id=progress_record.id,
        )
    )
    db_session.commit()
    assert cleanup_module.clear_database(sqlite_db_url) == (1, 0)


def test_cleanup_command_orchestrates_async_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exports_dir = tmp_path / "exports"
    xformed_dir = tmp_path / "xformed"
    exports_dir.mkdir()
    xformed_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    class FakeProgress:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def set_total_phases(self, count):
            return None

        def initialize_phases(self, phases):
            return None

        def start_phase(self, phase_id, description, total):
            return phase_id

        def update_phase(self, *args):
            return None

        def complete_phase(self, phase_id):
            return None

    class FakeTargetClient:
        def __init__(self, config=None, rate_limit=None):
            self.config = config
            self.rate_limit = rate_limit

    cleanup_callback = _unwrap_callback(cleanup_module.cleanup)
    info_messages = []
    warning_messages = []

    monkeypatch.setattr(cleanup_module, "MigrationProgressDisplay", FakeProgress)
    monkeypatch.setattr(cleanup_module, "AAPTargetClient", FakeTargetClient)
    monkeypatch.setattr(cleanup_module, "clear_database", lambda url: (4, 5))
    monkeypatch.setattr(
        cleanup_module,
        "cancel_all_jobs",
        lambda client, config: asyncio.sleep(0, result=(1, 0, 0, 0, 0)),
    )
    monkeypatch.setattr(
        cleanup_module,
        "get_cleanup_resource_types",
        lambda client, use_discovered=True: asyncio.sleep(0, result=["projects"]),
    )
    monkeypatch.setattr(
        cleanup_module,
        "fetch_counts_parallel",
        lambda client, resource_types: asyncio.sleep(0, result={"projects": 2}),
    )
    monkeypatch.setattr(
        cleanup_module,
        "delete_resources",
        lambda client, rtype, config, skip_default=True, progress_callback=None: asyncio.sleep(
            0, result=(2, 1, 1, [(9, "Bad", "blocked by jobs")])
        ),
    )
    monkeypatch.setattr(cleanup_module, "echo_info", lambda msg: info_messages.append(msg))
    monkeypatch.setattr(cleanup_module, "echo_warning", lambda msg: warning_messages.append(msg))

    ctx = SimpleNamespace(
        config=SimpleNamespace(
            paths=SimpleNamespace(export_dir=str(exports_dir)),
            performance=SimpleNamespace(rate_limit=7),
            target=SimpleNamespace(url="https://target.example.com"),
            state=SimpleNamespace(db_path=str(tmp_path / "state.db")),
        )
    )

    cleanup_callback(ctx, (), False, False, None, False, True, None, ())

    assert not exports_dir.exists()
    assert not xformed_dir.exists()
    assert any("Cleanup Summary:" in msg for msg in info_messages)
    assert any("blocked by active jobs" in msg.lower() for msg in warning_messages)
