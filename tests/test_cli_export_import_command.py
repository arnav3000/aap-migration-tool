from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

from aap_migration.cli.commands import export_import as command_module


def _unwrap_callback(command) -> object:
    callback = command.callback
    while hasattr(callback, "__wrapped__"):
        callback = callback.__wrapped__
    return callback


def test_dependency_helpers_and_pre_import_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        command_module,
        "get_importer_dependencies",
        lambda rtype: {
            "inventory_sources": {"inventory": "inventories", "credential": "credentials"},
            "inventories": {"organization": "organizations"},
            "credentials": {"credential_type": "credential_types"},
        }.get(rtype, {}),
    )
    monkeypatch.setattr(
        command_module,
        "RESOURCE_REGISTRY",
        {
            "organizations": SimpleNamespace(migration_order=1),
            "credential_types": SimpleNamespace(migration_order=2),
            "credentials": SimpleNamespace(migration_order=3),
            "inventories": SimpleNamespace(migration_order=4),
            "inventory_sources": SimpleNamespace(migration_order=5),
        },
    )

    closure = command_module.build_dependency_closure(
        ["inventory_sources"],
        ["organizations", "credential_types", "credentials", "inventories", "inventory_sources"],
    )
    assert closure == [
        "organizations",
        "credential_types",
        "credentials",
        "inventories",
        "inventory_sources",
    ]

    state = SimpleNamespace(
        get_import_stats=lambda rtype: (
            {"total_imported": 1} if rtype == "organizations" else {"total_imported": 0}
        )
    )
    missing = command_module.get_missing_dependencies(["organizations", "inventories"], state)
    assert missing == ["inventories"]

    input_dir = tmp_path / "xformed"
    team_dir = input_dir / "teams"
    project_dir = input_dir / "projects"
    team_dir.mkdir(parents=True)
    project_dir.mkdir(parents=True)
    (team_dir / "teams_001.json").write_text(json.dumps([{"id": 1}, {"id": 2}, {"id": 3}]))
    (project_dir / "projects_001.json").write_text(json.dumps({"id": 9}))
    (project_dir / "broken.json").write_text("{not-json")

    mapping_counts = {"teams": 1, "projects": 1}
    validation_state = SimpleNamespace(count_mapped_resources=lambda rtype: mapping_counts[rtype])
    warnings = []
    infos = []
    monkeypatch.setattr(command_module, "echo_warning", lambda msg: warnings.append(msg))
    monkeypatch.setattr(command_module, "echo_info", lambda msg: infos.append(msg))
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: False)

    should_continue, stats = command_module.validate_pre_import_state(
        input_dir, validation_state, yes=False
    )
    assert should_continue is False
    assert stats["transformed_count"] == 4
    assert stats["mapped_count"] == 2
    assert stats["missing_mappings"] == 2
    assert stats["resource_details"]["teams"]["missing"] == 2
    assert any("Pre-Import Validation Warning" in msg for msg in warnings)
    assert any("Missing mappings by resource type" in msg for msg in infos)


def test_export_command_sequential_and_parallel_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeState:
        def __init__(self) -> None:
            self.mapping_batches = []

        def batch_create_mappings(self, mappings, batch_size=100):
            self.mapping_batches.append((list(mappings), batch_size))

        def get_max_exported_id(self, rtype):
            return None

    class FakeProgress:
        def __init__(self, *args, **kwargs) -> None:
            self.started = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def set_total_phases(self, count):
            self.total = count

        def initialize_phases(self, phases):
            self.phases = phases

        def start_phase(self, phase_id, description, total):
            self.started.append((phase_id, description, total))
            return phase_id

        def update_phase(self, phase_id, completed, failed):
            return None

        def complete_phase(self, phase_id):
            return None

    class FakeExporter:
        def __init__(self, resource_type: str) -> None:
            self.resource_type = resource_type

        async def get_count(self, endpoint, filters=None):
            return {"organizations": 3, "parallel_type": 2}.get(self.resource_type, 0)

        async def export_parallel(self, resource_type, endpoint, page_size, max_concurrent_pages):
            for item in [
                {"id": 1, "name": "Org A"},
                {"id": 2, "name": "Org B"},
                {"id": 3, "name": "Org C"},
            ]:
                yield item

    class FakeCoordinator:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def export_all_parallel(self, resource_types, resume=False, progress_callback=None):
            progress_callback("parallel_type", {"exported": 2, "failed": 0})
            return {"parallel_type": {"exported": 2, "files_written": 1}}

    def fake_create_exporter(rtype, *args, **kwargs):
        if rtype == "noexport":
            raise NotImplementedError("not implemented")
        return FakeExporter(rtype)

    monkeypatch.setattr(command_module, "MigrationProgressDisplay", FakeProgress)
    monkeypatch.setattr(command_module, "create_exporter", fake_create_exporter)
    monkeypatch.setattr(command_module, "ParallelExportCoordinator", FakeCoordinator)
    monkeypatch.setattr(command_module, "normalize_resource_type", lambda rtype: rtype)
    monkeypatch.setattr(command_module, "get_endpoint", lambda rtype: f"{rtype}/")
    monkeypatch.setattr(command_module, "has_discovered_endpoints", lambda: False)
    monkeypatch.setattr(command_module, "READ_ONLY_ENDPOINTS", {"readonly"})
    monkeypatch.setattr(command_module, "RUNTIME_DATA_ENDPOINTS", {"runtime"})
    monkeypatch.setattr(command_module, "MANUAL_MIGRATION_ENDPOINTS", {"manual"})
    monkeypatch.setattr(command_module, "echo_info", lambda msg: None)
    monkeypatch.setattr(command_module, "echo_error", lambda msg: None)

    export_callback = _unwrap_callback(command_module.export)

    sequential_output = tmp_path / "exports-seq"
    sequential_ctx = SimpleNamespace(
        config=SimpleNamespace(
            paths=SimpleNamespace(export_dir=str(sequential_output)),
            export=SimpleNamespace(
                records_per_file=2, skip_dynamic_hosts=False, skip_smart_inventories=False
            ),
            performance=SimpleNamespace(
                parallel_resource_types=False,
                batch_sizes={},
                max_concurrent_pages=2,
                mapping_batch_size=2,
            ),
            source=SimpleNamespace(url="https://source.example.com"),
        ),
        source_client=object(),
        migration_state=FakeState(),
    )

    export_callback(
        sequential_ctx,
        sequential_output,
        ("readonly", "runtime", "manual", "noexport", "organizations"),
        False,
        2,
        False,
        True,
    )

    assert (sequential_output / "organizations" / "organizations_0001.json").exists()
    assert (sequential_output / "organizations" / "organizations_0002.json").exists()
    assert json.loads((sequential_output / "metadata.json").read_text())["total_resources"] == 3
    assert sequential_ctx.migration_state.mapping_batches

    parallel_output = tmp_path / "exports-par"
    parallel_ctx = SimpleNamespace(
        config=SimpleNamespace(
            paths=SimpleNamespace(export_dir=str(parallel_output)),
            export=SimpleNamespace(
                records_per_file=100, skip_dynamic_hosts=False, skip_smart_inventories=False
            ),
            performance=SimpleNamespace(
                parallel_resource_types=True,
                batch_sizes={},
                max_concurrent_pages=2,
                mapping_batch_size=2,
            ),
            source=SimpleNamespace(url="https://source.example.com"),
        ),
        source_client=object(),
        migration_state=FakeState(),
    )

    export_callback(
        parallel_ctx,
        parallel_output,
        ("parallel_type",),
        False,
        100,
        False,
        True,
    )
    assert json.loads((parallel_output / "metadata.json").read_text())["resource_types"][
        "parallel_type"
    ] == {
        "count": 2,
        "files": 1,
    }


def test_import_command_guards_and_dependency_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_callback = _unwrap_callback(command_module.import_cmd)

    class FakeTargetClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr("aap_migration.client.aap_target_client.AAPTargetClient", FakeTargetClient)

    base_ctx = SimpleNamespace(
        config=SimpleNamespace(
            paths=SimpleNamespace(transform_dir=str(tmp_path / "xformed")),
            target=SimpleNamespace(url="https://target.example.com"),
            performance=SimpleNamespace(
                rate_limit=10,
                http_max_connections=5,
                http_max_keepalive_connections=2,
            ),
            logging=SimpleNamespace(log_payloads=False, max_payload_size=1000),
        ),
        migration_state=SimpleNamespace(),
        _target_client=None,
    )

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "metadata.json").write_text(json.dumps({"resource_types": {"users": {"count": 1}}}))

    with pytest.raises(click.ClickException, match="transformed data"):
        import_callback(
            base_ctx,
            raw_dir,
            (),
            False,
            False,
            False,
            False,
            False,
            False,
            "all",
            True,
        )

    transformed_dir = tmp_path / "xformed"
    transformed_dir.mkdir()
    (transformed_dir / "metadata.json").write_text(
        json.dumps(
            {
                "transform_timestamp": "2026-05-18T12:00:00",
                "resource_types": {"inventories": {"count": 1}, "organizations": {"count": 1}},
            }
        )
    )

    calls = {}

    class FakeValidator:
        def __init__(self, state, input_dir):
            calls["init"] = (state, input_dir)

        def validate_all(self, requested):
            calls["requested"] = requested
            return {"ok": True}

        def display_validation_report(self, validation):
            calls["displayed"] = validation

    monkeypatch.setattr("aap_migration.validation.DependencyValidator", FakeValidator)

    import_callback(
        base_ctx,
        transformed_dir,
        ("inventories",),
        False,
        False,
        False,
        False,
        True,
        False,
        "all",
        True,
    )

    assert calls["init"][1] == transformed_dir
    assert calls["requested"] == ["inventories"]
    assert calls["displayed"] == {"ok": True}
