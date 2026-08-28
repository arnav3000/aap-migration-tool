from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import aap_migration.cli.granular_import as granular_module
import aap_migration.cli.import_menu as import_menu_module
import aap_migration.cli.menu as menu_module
from aap_migration.config import StateConfig
from aap_migration.migration.state import MigrationState


def build_ctx(sqlite_db_url: str, tmp_path: Path):
    state = MigrationState(StateConfig(db_path=sqlite_db_url), migration_id="cli-test")
    obj = SimpleNamespace(
        migration_state=state,
        config_path=tmp_path / "config.yaml",
        config=SimpleNamespace(paths=SimpleNamespace(transform_dir=tmp_path)),
    )
    return SimpleNamespace(obj=obj), state


def write_metadata(input_dir: Path, counts: dict[str, int]) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "metadata.json").write_text(
        json.dumps({"resource_types": {rtype: {"count": count} for rtype, count in counts.items()}})
    )


def test_granular_importer_helpers_and_micro_phase(sqlite_db_url, tmp_path, monkeypatch):
    ctx, state = build_ctx(sqlite_db_url, tmp_path)
    input_dir = tmp_path / "transformed"
    write_metadata(input_dir, {"organizations": 2})
    (input_dir / "settings").mkdir()
    (input_dir / "settings" / "settings.json").write_text("{}")

    state.create_source_mapping("organizations", 1, "Org One")
    state.create_source_mapping("organizations", 2, "Org Two")
    state.mark_in_progress("organizations", 1, "Org One")
    state.mark_completed("organizations", 1, 101, source_name="Org One")
    state.mark_in_progress("organizations", 2, "Org Two")
    state.mark_failed("organizations", 2, "boom")

    importer = granular_module.GranularImporter(ctx.obj, input_dir)
    assert importer.get_resource_count("organizations") == 2
    assert importer.get_resource_count("settings") == 1
    assert importer.get_import_stats("organizations") == {
        "total": 2,
        "completed": 1,
        "failed": 1,
        "pending": 1,
    }

    table = importer.create_phase_table(current_phase_id="1.1")
    assert table.row_count == len(granular_module.MICRO_PHASES)

    org_dir = input_dir / "organizations"
    org_dir.mkdir()
    (org_dir / "organizations_001.json").write_text("[]")

    stats = iter(
        [
            {"total": 2, "completed": 0, "failed": 0, "pending": 2},
            {"total": 2, "completed": 2, "failed": 0, "pending": 0},
        ]
    )
    monkeypatch.setattr(importer, "get_import_stats", lambda resource_type: next(stats))
    calls = []
    monkeypatch.setattr(
        granular_module.subprocess,
        "run",
        lambda cmd, check=False, capture_output=False, text=True: calls.append(cmd),
    )

    result = importer._import_micro_phase(
        {"id": "1.1", "name": "Organizations", "resource_type": "organizations"}
    )
    assert result == {"total": 2, "completed": 2, "failed": 0, "skipped": 0}
    assert calls[0][-5:] == ["migrate", "-r", "organizations", "--skip-prep", "--phase", "all"][-5:]


def test_granular_importer_run_auto_and_manual_paths(sqlite_db_url, tmp_path, monkeypatch):
    ctx, _state = build_ctx(sqlite_db_url, tmp_path)
    input_dir = tmp_path / "transformed"
    write_metadata(input_dir, {})

    monkeypatch.setattr(
        granular_module,
        "MICRO_PHASES",
        [
            {"id": "1.1", "name": "Organizations", "resource_type": "organizations"},
            {"id": "3.4", "name": "Inventory Sources", "resource_type": "inventory_sources"},
            {"id": "10.1", "name": "RBAC", "resource_type": "_rbac_manual", "manual": True},
        ],
    )

    auto_importer = granular_module.GranularImporter(ctx.obj, input_dir, auto_mode=True)
    monkeypatch.setattr(auto_importer, "create_phase_table", lambda current_phase_id=None: "TABLE")
    monkeypatch.setattr(
        auto_importer,
        "get_resource_count",
        lambda resource_type: 1 if resource_type in {"organizations", "inventory_sources"} else 0,
    )
    monkeypatch.setattr(
        auto_importer,
        "get_import_stats",
        lambda resource_type: {"total": 1, "completed": 0, "failed": 0, "pending": 1},
    )
    auto_calls = []
    monkeypatch.setattr(
        auto_importer,
        "_import_micro_phase",
        lambda micro_phase: (
            auto_calls.append(micro_phase["resource_type"]) or {"completed": 1, "failed": 0}
        ),
    )
    monkeypatch.setattr(granular_module.Prompt, "ask", lambda *args, **kwargs: "")
    auto_importer.run()
    assert auto_calls == ["organizations", "inventory_sources"]

    manual_importer = granular_module.GranularImporter(ctx.obj, input_dir, auto_mode=False)
    monkeypatch.setattr(
        manual_importer, "create_phase_table", lambda current_phase_id=None: "TABLE"
    )
    monkeypatch.setattr(manual_importer, "get_resource_count", lambda resource_type: 1)
    monkeypatch.setattr(
        manual_importer,
        "get_import_stats",
        lambda resource_type: {
            "total": 1,
            "completed": 0,
            "failed": 1 if resource_type == "organizations" else 0,
            "pending": 1,
        },
    )
    manual_calls = []
    monkeypatch.setattr(
        manual_importer,
        "_import_micro_phase",
        lambda micro_phase: (
            manual_calls.append(micro_phase["resource_type"]) or {"completed": 0, "failed": 1}
        ),
    )
    choices = iter(["", "v", "", "i", "n"])
    monkeypatch.setattr(granular_module.Prompt, "ask", lambda *args, **kwargs: next(choices))
    monkeypatch.setattr(
        "aap_migration.migration.database.get_session",
        lambda _db_url: FakeErrorSession([("1", "Org One", "boom")]),
    )
    monkeypatch.setattr(
        "aap_migration.migration.models.MigrationProgress",
        SimpleNamespace(
            resource_type="resource_type",
            status="status",
            source_id="source_id",
            source_name="source_name",
            error_message="error_message",
        ),
    )
    manual_importer.run()
    assert manual_calls == ["inventory_sources"]


def test_import_and_main_menu_helpers(sqlite_db_url, tmp_path, monkeypatch):
    ctx, state = build_ctx(sqlite_db_url, tmp_path)
    state.mark_in_progress("organizations", 1, "Org One")
    state.mark_completed("organizations", 1, 101, source_name="Org One")
    state.mark_in_progress("projects", 2, "Proj Two")
    state.mark_failed(
        "projects", 2, "failure details that are long enough to be truncated in the table"
    )
    state.create_or_update_mapping("inventory_sources", 3, None, source_name="Src Three")

    run_calls = []
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, check=False: (
            SimpleNamespace(returncode=0) if not run_calls.append(cmd) else None
        ),
    )
    assert import_menu_module.run_command(["import"], ctx) == 0

    monkeypatch.setattr(import_menu_module.Console, "print", lambda self, *args, **kwargs: None)
    import_menu_module.show_import_status(ctx)
    import_menu_module.show_failed_resources(ctx)

    submenu_calls = []
    monkeypatch.setattr(
        import_menu_module,
        "run_command",
        lambda args, ctx=None: submenu_calls.append(tuple(args)) or 0,
    )
    monkeypatch.setattr(
        import_menu_module, "granular_import_menu", lambda ctx: submenu_calls.append(("granular",))
    )
    monkeypatch.setattr(
        import_menu_module, "show_import_status", lambda ctx: submenu_calls.append(("status",))
    )
    submenu_inputs = iter(["1", "", "2", "y", "", "3", "y", "", "4", "", "b"])
    monkeypatch.setattr(
        import_menu_module.Prompt, "ask", lambda *args, **kwargs: next(submenu_inputs)
    )
    monkeypatch.setattr(import_menu_module.Console, "clear", lambda self: None)
    monkeypatch.setattr(import_menu_module.Console, "print", lambda self, *args, **kwargs: None)
    import_menu_module.import_submenu(ctx)
    assert submenu_calls == [
        ("import", "--check-dependencies"),
        ("import",),
        ("granular",),
        ("status",),
    ]

    main_calls = []
    monkeypatch.setattr(
        menu_module, "run_command", lambda args, ctx=None: main_calls.append(tuple(args))
    )
    monkeypatch.setattr(
        menu_module, "import_submenu", lambda ctx: main_calls.append(("import-submenu",))
    )
    menu_inputs = iter(["1", "", "2", "", "3", "", "4", "5", "", "q"])
    monkeypatch.setattr(menu_module.Prompt, "ask", lambda *args, **kwargs: next(menu_inputs))
    monkeypatch.setattr(menu_module.Console, "clear", lambda self: None)
    monkeypatch.setattr(menu_module.Console, "print", lambda self, *args, **kwargs: None)
    menu_module.interactive_menu(ctx)
    assert main_calls == [
        ("prep",),
        ("export",),
        ("transform",),
        ("import-submenu",),
        ("cleanup",),
    ]


def test_import_and_main_menu_error_branches(sqlite_db_url, tmp_path, monkeypatch):
    ctx, _state = build_ctx(sqlite_db_url, tmp_path)
    error_messages = []
    console_output = []
    printed = []

    monkeypatch.setattr(
        import_menu_module, "echo_error", lambda message: error_messages.append(message)
    )
    monkeypatch.setattr(
        import_menu_module.Console,
        "print",
        lambda self, *args, **kwargs: console_output.append(args),
    )

    def raising_run(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("subprocess.run", raising_run)
    assert import_menu_module.run_command(["import"], ctx) == 1

    ctx.obj.migration_state = SimpleNamespace(
        database_url="sqlite:///fake.db",
        get_import_stats=lambda _rtype: {"total_exported": 0, "pending": 0, "percent_complete": 0},
    )
    monkeypatch.setattr(
        "aap_migration.migration.database.get_session", lambda _db_url: FakeErrorSession([])
    )
    import_menu_module.show_import_status(ctx)
    assert any("No import progress found" in str(arg[0]) for arg in console_output if arg)

    monkeypatch.setattr(
        "aap_migration.migration.database.get_session",
        lambda _db_url: (_ for _ in ()).throw(RuntimeError("status failed")),
    )
    import_menu_module.show_import_status(ctx)
    assert any("Failed to get import status" in message for message in error_messages)

    console_output.clear()
    monkeypatch.setattr(
        "aap_migration.migration.database.get_session", lambda _db_url: FakeErrorSession([])
    )
    import_menu_module.show_failed_resources(ctx)
    assert any("No failed resources" in str(arg[0]) for arg in console_output if arg)

    monkeypatch.setattr(
        "aap_migration.migration.database.get_session",
        lambda _db_url: (_ for _ in ()).throw(RuntimeError("details failed")),
    )
    import_menu_module.show_failed_resources(ctx)
    assert any("Failed to get error details" in message for message in error_messages)

    monkeypatch.setattr(menu_module.subprocess, "run", raising_run)
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(args))
    menu_module.run_command(["prep"], ctx)
    assert any("Error running command: boom" in str(arg[0]) for arg in printed if arg)


class FakeErrorQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *args, **kwargs):
        return self

    def limit(self, value):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self.rows


class FakeErrorSession:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def query(self, *args, **kwargs):
        return FakeErrorQuery(self.rows)
