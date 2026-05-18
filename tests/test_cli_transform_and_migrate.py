from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import click

from aap_migration.cli.commands import migrate as migrate_module
from aap_migration.cli.commands import transform as transform_module
from aap_migration.migration.transformer import SkipResourceError


def _unwrap_callback(command) -> object:
    callback = command.callback
    while hasattr(callback, "__wrapped__"):
        callback = callback.__wrapped__
    return callback


def test_transform_command_sequential_and_parallel(tmp_path: Path, monkeypatch) -> None:
    input_dir = tmp_path / "exports"
    output_dir = tmp_path / "xformed"
    schema_dir = tmp_path / "schemas"
    input_dir.mkdir()
    schema_dir.mkdir()
    (schema_dir / "schema_comparison.json").write_text("{}")
    (input_dir / "metadata.json").write_text(
        json.dumps(
            {
                "records_per_file": 1000,
                "resource_types": {
                    "inventories": {"count": 2},
                    "credential_types": {"count": 2},
                    "hosts": {"count": 2},
                    "credentials": {"count": 2},
                    "parallel_type": {"count": 1},
                },
            }
        )
    )

    for rtype, payload in {
        "inventories": [
            {"id": 1, "name": "Delete Me", "pending_deletion": True},
            {"id": 2, "name": "Inventory"},
        ],
        "credential_types": [
            {
                "id": 11,
                "name": "Managed",
                "managed": True,
                "related": {"created_by": "/api/v2/users/1/"},
            },
            {"id": 12, "name": "Builtin", "managed": True},
        ],
        "hosts": [
            {"id": 21, "name": "Dynamic", "has_inventory_sources": True, "inventory": 99},
            {"id": 22, "name": "Host", "inventory": 10},
        ],
        "credentials": [
            {"id": 31, "name": "Null Org", "organization": None},
            {"id": 32, "name": "Skip Credential", "organization": 1},
        ],
    }.items():
        resource_dir = input_dir / rtype
        resource_dir.mkdir()
        (resource_dir / f"{rtype}_0001.json").write_text(json.dumps(payload))

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

    class FakeTransformer:
        def __init__(self, resource_type: str) -> None:
            self.resource_type = resource_type

        def transform_resource(self, resource_type: str, resource: dict) -> dict:
            if resource_type == "credentials" and resource["name"] == "Skip Credential":
                raise SkipResourceError(
                    "missing dependency",
                    resource_type="credentials",
                    source_id=resource["id"],
                    missing_dependency="organizations:1",
                )
            transformed = dict(resource)
            transformed["transformed"] = True
            if resource_type == "credentials":
                transformed.pop("organization", None)
            return transformed

        async def populate_target_id_from_target(self, resource, target_client, state, source_id):
            state.populated.append((resource["name"], source_id))

    class FakeParallelCoordinator:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def transform_all_parallel(self, resource_types, progress_callback):
            progress_callback(
                "parallel_type", {"count": 1, "failed": 0, "skipped_from_transformer": 1}
            )
            return {
                "parallel_type": {
                    "count": 1,
                    "files": 1,
                    "failed": 0,
                    "skipped_pending_deletion": 0,
                    "skipped_custom_managed": 0,
                    "skipped_missing_inventory": 0,
                    "skipped_smart_inventories": 0,
                    "skipped_dynamic_hosts": 0,
                    "skipped_from_transformer": 1,
                    "fields_removed": 1,
                    "fields_added": 2,
                    "fields_renamed": 3,
                }
            }

    class FakeState:
        def __init__(self) -> None:
            self.skipped = []
            self.populated = []

        def get_all_source_ids(self, resource_type):
            return []

        def mark_transform_skipped(self, **kwargs):
            self.skipped.append(kwargs)

        def has_source_mapping(self, resource_type, source_id):
            return source_id == 10

    monkeypatch.setattr(transform_module, "MigrationProgressDisplay", FakeProgress)
    monkeypatch.setattr(transform_module, "ParallelTransformCoordinator", FakeParallelCoordinator)
    monkeypatch.setattr(
        transform_module,
        "create_transformer",
        lambda resource_type, **kwargs: FakeTransformer(resource_type),
    )
    monkeypatch.setattr(transform_module, "echo_info", lambda msg: None)
    monkeypatch.setattr(transform_module, "echo_warning", lambda msg: None)

    transform_callback = _unwrap_callback(transform_module.transform)

    sequential_ctx = SimpleNamespace(
        config=SimpleNamespace(
            paths=SimpleNamespace(
                export_dir=str(input_dir),
                transform_dir=str(output_dir),
                schema_dir=str(schema_dir),
            ),
            performance=SimpleNamespace(parallel_resource_types=False),
            transform=SimpleNamespace(skip_pending_deletion=True),
        ),
        config_path=tmp_path / "config.yml",
        migration_state=FakeState(),
        target_client=object(),
    )

    transform_callback(
        sequential_ctx,
        input_dir,
        output_dir,
        schema_dir / "schema_comparison.json",
        False,
        ("inventories", "credential_types", "hosts", "credentials"),
        False,
        False,
        True,
        True,
        True,
    )

    transformed_metadata = json.loads((output_dir / "metadata.json").read_text())
    assert transformed_metadata["total_resources"] == 4
    assert transformed_metadata["resource_types"]["credentials"]["skipped_from_transformer"] == 1
    assert sequential_ctx.migration_state.skipped
    assert ("Builtin", 12) in sequential_ctx.migration_state.populated
    assert ("Null Org", 31) in sequential_ctx.migration_state.populated

    parallel_output = tmp_path / "parallel-xformed"
    parallel_ctx = SimpleNamespace(
        config=SimpleNamespace(
            paths=SimpleNamespace(
                export_dir=str(input_dir),
                transform_dir=str(parallel_output),
                schema_dir=str(schema_dir),
            ),
            performance=SimpleNamespace(parallel_resource_types=True),
            transform=SimpleNamespace(skip_pending_deletion=True),
        ),
        config_path=tmp_path / "config.yml",
        migration_state=FakeState(),
        target_client=object(),
    )

    transform_callback(
        parallel_ctx,
        input_dir,
        parallel_output,
        schema_dir / "schema_comparison.json",
        False,
        ("parallel_type",),
        False,
        False,
        True,
        True,
        True,
    )
    assert (
        json.loads((parallel_output / "metadata.json").read_text())["resource_types"][
            "parallel_type"
        ]["skipped_from_transformer"]
        == 1
    )


def test_migrate_helpers_and_workflow(tmp_path: Path, monkeypatch) -> None:
    class FakeClient:
        def __init__(self, results):
            self.results = results

        async def get(self, endpoint, params=None):
            return {"results": self.results}

    mapped = []
    state = SimpleNamespace(
        create_or_update_mapping=lambda **kwargs: mapped.append(kwargs),
        database_url="sqlite:///ignored.db",
    )
    mapped_count = click.utils.LazyFile  # type: ignore[assignment]
    # async helper
    import asyncio

    mapped_count = asyncio.run(
        migrate_module._map_managed_credential_types(
            FakeClient([{"id": 1, "name": "Machine"}, {"id": 2, "name": "Missing"}]),
            FakeClient([{"id": 101, "name": "Machine"}]),
            state,
        )
    )
    assert mapped_count == 1
    assert mapped[0]["target_id"] == 101

    xformed_dir = tmp_path / "xformed"
    inv_dir = xformed_dir / "inventory_sources"
    inv_dir.mkdir(parents=True)
    (inv_dir / "inventory_sources_0001.json").write_text(
        json.dumps(
            [
                {"name": "scm one", "source": "scm", "source_project": 7},
                {"name": "ec2", "source": "ec2", "source_project": 8},
                {"name": "scm two", "source": "scm", "source_project": 9},
            ]
        )
    )
    assert migrate_module._scan_scm_inventory_source_projects(xformed_dir) == {7, 9}

    calls = []

    def record(name):
        def inner(**kwargs):
            calls.append((name, kwargs))

        return inner

    prep_cmd = click.command()(record("prep"))
    export_cmd = click.command()(record("export"))
    transform_cmd = click.command()(record("transform"))
    import_cmd = click.command()(record("import"))

    async def fake_patch_project_scm_details(
        ctx, xformed_dir, batch_size, interval, project_source_ids=None
    ):
        calls.append(
            (
                "patch",
                {
                    "xformed_dir": xformed_dir,
                    "batch_size": batch_size,
                    "interval": interval,
                    "project_source_ids": project_source_ids,
                },
            )
        )

    class FakeAAPClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def count(self):
            return 0

        def distinct(self):
            return self

        def all(self):
            return []

    class FakeSession:
        def query(self, *args, **kwargs):
            return FakeQuery()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("aap_migration.cli.commands.prep.prep", prep_cmd)
    monkeypatch.setattr("aap_migration.cli.commands.export_import.export", export_cmd)
    monkeypatch.setattr("aap_migration.cli.commands.transform.transform", transform_cmd)
    monkeypatch.setattr("aap_migration.cli.commands.export_import.import_cmd", import_cmd)
    monkeypatch.setattr(
        "aap_migration.cli.commands.patch_projects.patch_project_scm_details",
        fake_patch_project_scm_details,
    )
    monkeypatch.setattr(
        "aap_migration.resources.get_exportable_types",
        lambda use_discovered=True: ["inventory_sources", "job_templates"],
    )
    monkeypatch.setattr(
        "aap_migration.resources.get_importable_types",
        lambda use_discovered=True: ["inventory_sources", "job_templates"],
    )
    monkeypatch.setattr("aap_migration.client.aap_target_client.AAPTargetClient", FakeAAPClient)
    monkeypatch.setattr("aap_migration.client.aap_source_client.AAPSourceClient", FakeAAPClient)
    monkeypatch.setattr(
        "aap_migration.migration.database.get_session", lambda database_url: FakeSession()
    )
    monkeypatch.setattr(migrate_module, "_scan_scm_inventory_source_projects", lambda path: {10})
    monkeypatch.setattr(migrate_module, "echo_info", lambda msg: None)
    monkeypatch.setattr(migrate_module, "echo_success", lambda msg: None)
    monkeypatch.setattr(migrate_module, "echo_warning", lambda msg: None)
    monkeypatch.setattr(migrate_module, "echo_error", lambda msg: None)

    ctx = SimpleNamespace(
        config=SimpleNamespace(
            target=SimpleNamespace(url="https://target.example.com"),
            source=SimpleNamespace(url="https://source.example.com"),
            performance=SimpleNamespace(
                rate_limit=10,
                http_max_connections=5,
                http_max_keepalive_connections=2,
                project_patch_batch_size=3,
                project_patch_batch_interval=0,
            ),
            logging=SimpleNamespace(log_payloads=False, max_payload_size=1000),
        ),
        migration_state=SimpleNamespace(database_url="sqlite:///ignored.db"),
    )

    migrate_module._run_migration_workflow(
        ctx,
        resource_type=("inventory_sources", "job_templates"),
        force=False,
        resume=False,
        skip_prep=False,
        phase="all",
    )

    assert [name for name, _ in calls[:4]] == ["prep", "export", "transform", "import"]
    assert any(name == "patch" and data["project_source_ids"] == {10} for name, data in calls)
    assert any(name == "import" and data["phase"] == "phase3" for name, data in calls)


def test_migrate_commands_status_resume_and_group(monkeypatch) -> None:
    status_callback = _unwrap_callback(migrate_module.status)
    resume_callback = _unwrap_callback(migrate_module.resume)

    outputs = []
    monkeypatch.setattr(migrate_module, "echo_info", lambda msg: outputs.append(("info", msg)))
    monkeypatch.setattr(
        migrate_module, "echo_success", lambda msg: outputs.append(("success", msg))
    )
    monkeypatch.setattr(
        migrate_module, "echo_warning", lambda msg: outputs.append(("warning", msg))
    )
    monkeypatch.setattr(migrate_module, "echo_error", lambda msg: outputs.append(("error", msg)))
    monkeypatch.setattr(
        migrate_module, "print_table", lambda *args, **kwargs: outputs.append(("table", args[0]))
    )
    monkeypatch.setattr(
        migrate_module, "print_stats", lambda stats, title: outputs.append(("stats", title))
    )

    status_ctx = SimpleNamespace(migration_state=SimpleNamespace(migration_id="mig-1"))
    status_callback(status_ctx)
    assert ("info", "Migration Status") in outputs
    assert ("table", "Migration Phases") in outputs

    class FakeProgressBar:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def add_task(self, description, total):
            return "task"

        def update(self, task, description):
            return None

        def advance(self, task):
            return None

    monkeypatch.setattr(migrate_module, "create_progress_bar", lambda label: FakeProgressBar())
    monkeypatch.setattr(migrate_module, "MigrationCoordinator", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        "aap_migration.utils.logging.configure_logging",
        lambda level, log_format: outputs.append(("configured", level)),
    )

    resume_ctx = SimpleNamespace(
        config=SimpleNamespace(logging=SimpleNamespace(format="json")),
        source_client=object(),
        target_client=object(),
        migration_state=SimpleNamespace(),
    )
    resume_callback(resume_ctx, "projects", False, False, False)
    assert any(
        item[0] == "success" and "resumed and completed" in item[1].lower() for item in outputs
    )

    group_ctx = SimpleNamespace(
        invoked_subcommand=None, obj=SimpleNamespace(config=SimpleNamespace())
    )
    called = []
    monkeypatch.setattr(
        migrate_module, "_run_migration_workflow", lambda *args, **kwargs: called.append(kwargs)
    )
    migrate_group_callback = _unwrap_callback(migrate_module.migrate)
    migrate_group_callback(group_ctx, ("projects",), False, True, False, "all")
    assert called[0]["resume"] is True
