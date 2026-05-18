from __future__ import annotations

import logging

import pytest

from aap_migration.prep.endpoint_discovery import (
    discover_endpoints,
    load_endpoints,
    save_endpoints,
)
from aap_migration.prep.schema_comparison import (
    compare_schemas,
    load_comparison,
    save_comparison,
)
from aap_migration.prep.schema_generator import (
    fetch_endpoint_schema,
    generate_schema,
    load_schema,
    save_schema,
)
from aap_migration.reporting.live_progress import (
    MigrationProgressDisplay,
    PhaseProgressState,
    StatusIconColumn,
)
from aap_migration.reporting.progress import LiveStats, ProgressTracker
from aap_migration.reporting.progress_orchestrator import (
    DisabledPhaseTracker,
    OrchestratorResult,
    ProgressOrchestrator,
)


class FakeClient:
    def __init__(self, root=None, options_map=None):
        self.base_url = "https://example.test/api/v2"
        self._root = root or {}
        self._options_map = options_map or {}

    async def get(self, endpoint: str):
        assert endpoint == ""
        return self._root

    async def options(self, endpoint: str, suppress_server_error: bool = False):
        assert suppress_server_error is True
        value = self._options_map[endpoint]
        if isinstance(value, Exception):
            raise value
        return value


class FakeTqdm:
    def __init__(self, total, desc, unit, position, leave, bar_format):
        self.total = total
        self.desc = desc
        self.unit = unit
        self.position = position
        self.leave = leave
        self.bar_format = bar_format
        self.updated = []
        self.postfixes = []
        self.closed = False

    def update(self, value):
        self.updated.append(value)

    def set_postfix(self, **kwargs):
        self.postfixes.append(kwargs)

    def set_description(self, description):
        self.desc = description

    def close(self):
        self.closed = True


class FakeProgressDisplay:
    def __init__(self, title: str, enabled: bool, show_stats: bool):
        self.title = title
        self.enabled = enabled
        self.show_stats = show_stats
        self.calls = []
        self.phase_states = {"phase1": type("State", (), {"success_count": 3, "failed": 1})()}

    def __enter__(self):
        self.calls.append(("enter",))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.calls.append(("exit",))
        return None

    def initialize_phases(self, phases):
        self.calls.append(("initialize", phases))

    def set_total_phases(self, total):
        self.calls.append(("total", total))

    def start_phase(self, phase_id, description, total):
        self.calls.append(("start", phase_id, description, total))

    def update_phase(self, phase_id, completed, failed=0):
        self.calls.append(("update", phase_id, completed, failed))

    def complete_phase(self, phase_id):
        self.calls.append(("complete", phase_id))


class FakeExporter:
    def __init__(self, client, state, performance_config):
        self.client = client
        self.state = state
        self.performance_config = performance_config

    async def get_count(self, endpoint):
        if endpoint == "broken/":
            raise RuntimeError("boom")
        return 7 if endpoint == "ok/" else 3


@pytest.mark.asyncio
async def test_endpoint_and_schema_prep_roundtrip(tmp_path) -> None:
    root = {
        "description": "AAP",
        "organizations": "/api/v2/organizations/",
        "metrics": "/api/v2/metrics/",
        "users": "/api/v2/users/",
    }
    source_client = FakeClient(
        root=root,
        options_map={
            "organizations/": {
                "actions": {
                    "POST": {
                        "name": {"type": "string", "required": True, "max_length": 64},
                        "description": {"type": "string", "default": ""},
                    },
                    "GET": {
                        "id": {"type": "integer", "help_text": "identifier"},
                        "name": {"type": "string"},
                    },
                }
            },
            "users/": {
                "actions": {
                    "POST": {
                        "username": {"type": "string", "required": True},
                    }
                }
            },
        },
    )

    endpoints = await discover_endpoints(source_client, "2.3.0", ignored_endpoints=["metrics/"])
    assert sorted(endpoints["endpoints"]) == ["organizations", "users"]
    assert endpoints["endpoints"]["organizations"]["url"] == "organizations/"

    endpoints_path = tmp_path / "prep" / "endpoints.json"
    save_endpoints(endpoints, endpoints_path)
    assert load_endpoints(endpoints_path)["api_version"] == "2.3.0"

    schema = await generate_schema(source_client, endpoints)
    assert schema["schemas"]["organizations"]["fields"]["name"]["required"] is True
    assert schema["schemas"]["organizations"]["fields"]["id"]["read_only"] is True

    schema_path = tmp_path / "prep" / "schema.json"
    save_schema(schema, schema_path)
    assert load_schema(schema_path)["schemas"]["users"]["fields"]["username"]["type"] == "string"

    target_schema = {
        "api_version": "2.6.0",
        "schemas": {
            "organizations": {
                "fields": {
                    "name": {"type": "string", "required": True},
                    "description": {"type": "text", "required": False},
                    "is_default": {"type": "boolean", "required": True, "default": False},
                }
            },
            "extra_only": {"fields": {"name": {"type": "string"}}},
            "users": {"fields": {}},
        },
    }
    comparison = compare_schemas(schema, target_schema)

    assert comparison["transformations"]["organizations"]["fields_added"] == ["is_default"]
    assert comparison["transformations"]["organizations"]["fields_type_changed"] == {
        "description": {"source_type": "string", "target_type": "text"}
    }
    assert comparison["transformations"]["organizations"]["new_required_defaults"] == {
        "is_default": False
    }
    assert comparison["transformations"]["users"]["requires_manual_verification"] is True
    assert "extra_only" not in comparison["transformations"]

    comparison_path = tmp_path / "prep" / "comparison.json"
    save_comparison(comparison, comparison_path)
    assert load_comparison(comparison_path)["target_version"] == "2.6.0"


@pytest.mark.asyncio
async def test_fetch_endpoint_schema_handles_errors() -> None:
    class ResponseError(Exception):
        def __init__(self, status_code: int):
            self.response = type("Resp", (), {"status_code": status_code})()
            super().__init__(f"[{status_code}] failure")

    client = FakeClient(
        options_map={
            "ok/": {
                "actions": {
                    "POST": {"name": {"type": "string", "required": True, "choices": ["a"]}},
                    "GET": {"id": {"type": "integer"}},
                }
            },
            "five-hundred/": ResponseError(500),
            "four-hundred/": ResponseError(404),
            "empty/": {"actions": {}},
        }
    )

    assert (await fetch_endpoint_schema(client, "ok/")) == {
        "name": {
            "type": "string",
            "required": True,
            "read_only": False,
            "help_text": "",
            "choices": ["a"],
        },
        "id": {
            "type": "integer",
            "required": False,
            "read_only": True,
            "help_text": "",
        },
    }
    assert await fetch_endpoint_schema(client, "five-hundred/") is None
    assert await fetch_endpoint_schema(client, "four-hundred/") is None
    assert await fetch_endpoint_schema(client, "empty/") is None


def test_progress_tracker_and_live_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aap_migration.reporting.progress.tqdm", FakeTqdm)

    tracker = ProgressTracker(total_phases=2, enable=True)
    tracker.start_phase("organizations", total_resources=5)
    tracker.update_resource(exported=2, transformed=1, imported=1, failed=1, skipped=1)
    tracker.set_phase_description("Organizations Updated")
    tracker.complete_phase()
    tracker.close()

    stats = tracker.get_stats()
    assert stats == {
        "phases_completed": 1,
        "resources_exported": 2,
        "resources_transformed": 1,
        "resources_imported": 1,
        "resources_failed": 1,
        "resources_skipped": 1,
    }
    assert tracker.phase_bar is None
    assert tracker.resource_bar is None

    live_stats = LiveStats(enable=True)
    logged = []
    monkeypatch.setattr(
        "aap_migration.reporting.progress.logger.info",
        lambda event, **kw: logged.append((event, kw)),
    )
    live_stats.update(current_phase="organizations", resources_imported=9)
    assert live_stats.get_summary()["resources_imported"] == 9
    assert logged and logged[0][0] == "live_stats"


def test_live_progress_display_lifecycle_and_phase_state() -> None:
    state = PhaseProgressState("phase1", "Organizations", 5, completed=1, failed=1, skipped=1)
    assert state.success_count == 0
    assert state.progress_percentage == 20.0
    assert state.status_text == "running"
    assert "Err:1" in state.formatted_metrics

    empty = PhaseProgressState("phase2", "Empty", 0)
    assert empty.progress_percentage == 100.0
    assert empty.status_text == "complete"

    column = StatusIconColumn()
    assert (
        str(column.render(type("Task", (), {"fields": {"status_text": "complete"}})()).plain) == "✓"
    )
    assert (
        str(column.render(type("Task", (), {"fields": {"status_text": "pending"}})()).plain) == "•"
    )

    class RichHandler(logging.Handler):
        pass

    root_logger = logging.getLogger()
    handler = RichHandler()
    root_logger.addHandler(handler)
    try:
        display = MigrationProgressDisplay(enabled=True, title="Test")
        started = {"value": False}
        stopped = {"value": False}
        display.live.start = lambda: started.__setitem__("value", True)
        display.live.stop = lambda: stopped.__setitem__("value", True)

        display.start()
        assert handler not in root_logger.handlers

        display.set_total_phases(2)
        display.initialize_phases([("phase1", "Organizations", 3)])
        phase_id = display.start_phase("phase1", "Organizations", 3)
        display.update_phase(phase_id, completed=2, failed=1, skipped=1)
        display.complete_phase(phase_id)
        single_id = display.initialize_and_start_single_phase("phase2", "Users", 2)

        assert started["value"] is True
        assert phase_id == "phase1"
        assert single_id == "phase2"
        assert display.phase_states["phase1"].status_text == "complete_with_issues"

        display.stop()
        assert stopped["value"] is True
        assert handler in root_logger.handlers
    finally:
        if handler in root_logger.handlers:
            root_logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_progress_orchestrator_and_disabled_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "aap_migration.reporting.progress_orchestrator.MigrationProgressDisplay",
        FakeProgressDisplay,
    )

    orchestrator = ProgressOrchestrator(title="Export", enabled=True, show_stats=True)
    phases = await orchestrator.prefetch_counts(
        client=object(),
        phase_configs=[
            ("phase1", FakeExporter, "ok/"),
            ("phase2", FakeExporter, "broken/"),
        ],
        state=object(),
        performance_config=object(),
    )
    assert phases == [("phase1", "Phase1", 7), ("phase2", "Phase2", 0)]
    assert orchestrator.result.errors == ["Failed to fetch count for phase2: boom"]

    with orchestrator.progress_context(phases) as tracker:
        tracker.start_phase("phase1", "Phase1", 4)
        tracker.update("phase1", completed=4, failed=1)
        tracker.complete_phase("phase1")

    assert orchestrator._progress is None
    assert orchestrator.result.total_resources == 3
    assert orchestrator.result.total_failed == 1
    assert orchestrator.result.phase_stats["phase1"].completed == 4

    result = OrchestratorResult()
    disabled = DisabledPhaseTracker(result)
    phase_id = disabled.start_phase("phase3", "Phase3", 5)
    disabled.update(phase_id, completed=2, failed=1)
    disabled.complete_phase(phase_id, failed=2)
    assert result.phase_stats["phase3"].success == 3
