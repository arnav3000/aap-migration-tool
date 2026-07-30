from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from aap_migration.cli.commands.credentials import credentials
from aap_migration.cli.commands.state import state
from aap_migration.cli.context import MigrationContext
from aap_migration.reporting.enhanced_progress import EnhancedProgressDisplay


class FakeMigrationState:
    migration_id = "mig-123"

    def get_mapped_id(self, resource_type, source_id):
        if resource_type == "hosts" and source_id == 123:
            return 456
        return None


def build_ctx(tmp_path: Path) -> MigrationContext:
    ctx = MigrationContext(config_path=tmp_path / "config.yaml")
    ctx._config = SimpleNamespace(
        state=SimpleNamespace(db_path=str(tmp_path / "state.db")),
        dry_run=False,
    )
    ctx._source_client = object()
    ctx._target_client = object()
    ctx._migration_state = FakeMigrationState()
    return ctx


class FakeCoordinator:
    compare_result = {
        "total_source": 4,
        "total_target": 2,
        "matching_count": 1,
        "managed_skipped": 1,
        "missing_count": 2,
        "missing_credentials": [
            {"source_id": 1, "name": "Machine", "type": "Machine", "organization": "Default"},
            {"source_id": 2, "name": "Vault", "type": "Vault", "organization": None},
        ],
    }
    migrate_result = {
        "total_resources_exported": 5,
        "total_resources_imported": 4,
        "total_resources_failed": 1,
        "total_resources_skipped": 0,
        "report_files": ["report-a.md", "report-b.json"],
    }

    def __init__(self, config, source_client, target_client, state, enable_progress):
        self.config = config

    async def compare_and_verify_credentials(self, report_path: str):
        return dict(self.compare_result)

    async def migrate_all(self, only_phases, generate_report, report_dir):
        return dict(self.migrate_result)


def test_credentials_commands_and_state_commands(tmp_path, monkeypatch) -> None:
    runner = CliRunner()
    ctx = build_ctx(tmp_path)

    monkeypatch.setattr(
        "aap_migration.cli.commands.credentials.MigrationCoordinator",
        FakeCoordinator,
    )
    monkeypatch.setattr(
        "aap_migration.cli.commands.credentials.print_table",
        lambda title, headers, rows: __import__("click").echo(f"{title}:{len(rows)}"),
    )
    monkeypatch.setattr(
        "aap_migration.cli.commands.state.print_table",
        lambda title, headers, rows: __import__("click").echo(f"{title}:{len(rows)}"),
    )
    monkeypatch.setattr(
        "aap_migration.cli.commands.state.print_stats",
        lambda stats, title="Statistics": __import__("click").echo(
            f"{title}:{stats['id_mappings_stored']}"
        ),
    )

    compare = runner.invoke(credentials, ["compare", "--output", "out.md"], obj=ctx)
    assert compare.exit_code == 0
    assert "Credential Comparison Complete!" in compare.output
    assert "Missing Credentials:2" in compare.output
    assert "Next step: Run 'aap-bridge migrate credentials'" in compare.output

    report = runner.invoke(credentials, ["report", "--output", "status.md"], obj=ctx)
    assert report.exit_code == 0
    assert "Credential report generated: status.md" in report.output
    assert "Missing in Target: 2" in report.output

    migrate = runner.invoke(
        credentials, ["migrate", "--dry-run", "--report-dir", "reports"], obj=ctx
    )
    assert migrate.exit_code == 0
    assert "DRY RUN MODE - No changes will be made" in migrate.output
    assert "Credential Migration Complete!" in migrate.output
    assert "Failed resources: 1" in migrate.output
    assert "report-a.md" in migrate.output

    FakeCoordinator.compare_result = {
        "total_source": 1,
        "total_target": 1,
        "matching_count": 1,
        "managed_skipped": 0,
        "missing_count": 0,
        "missing_credentials": [],
    }
    no_action = runner.invoke(credentials, ["migrate", "--report-dir", "reports"], obj=ctx)
    assert no_action.exit_code == 0
    assert "All credentials already exist in target!" in no_action.output

    FakeCoordinator.compare_result = {
        "total_source": 4,
        "total_target": 2,
        "matching_count": 1,
        "managed_skipped": 1,
        "missing_count": 2,
        "missing_credentials": [
            {"source_id": 1, "name": "Machine", "type": "Machine", "organization": "Default"},
            {"source_id": 2, "name": "Vault", "type": "Vault", "organization": None},
        ],
    }

    show = runner.invoke(state, ["show", "--detailed"], obj=ctx)
    assert show.exit_code == 0
    assert "Migration ID: mig-123" in show.output
    assert "Migrated Resources:6" in show.output
    assert "Detailed view not yet implemented" in show.output

    mapping = runner.invoke(
        state,
        ["mappings", "--resource-type", "hosts", "--source-id", "123"],
        obj=ctx,
    )
    assert mapping.exit_code == 0
    assert "Target ID: 456" in mapping.output
    assert "Mapping found" in mapping.output

    no_mapping = runner.invoke(
        state,
        ["mappings", "--resource-type", "hosts", "--source-id", "999"],
        obj=ctx,
    )
    assert no_mapping.exit_code == 0
    assert "No mapping found for hosts ID 999" in no_mapping.output

    list_mapping = runner.invoke(state, ["mappings"], obj=ctx)
    assert list_mapping.exit_code == 0
    assert "No ID mappings found" in list_mapping.output

    reset = runner.invoke(
        state, ["reset", "--resource-type", "hosts", "--keep-mappings", "--yes"], obj=ctx
    )
    assert reset.exit_code == 0
    assert "Reset state for hosts" in reset.output
    assert "ID mappings will be preserved" in reset.output

    export = runner.invoke(state, ["export", "--output", "state.json"], obj=ctx)
    assert export.exit_code == 0
    assert "Exported migration state to state.json" in export.output


def test_enhanced_progress_display_tracks_totals_and_errors(monkeypatch) -> None:
    display = EnhancedProgressDisplay(FakeMigrationState())
    display.add_resource_type("projects", 3)
    display.add_resource_type("users", 1)
    display.update_progress("projects", advance=1)
    display.update_progress("projects", failed=1)
    display.add_error("users", 7, "User A", "x" * 60)

    summary = display.create_summary_panel()
    errors = display.create_errors_panel()
    layout = display.get_display_layout()

    assert display.totals == {"total": 4, "completed": 1, "failed": 2, "pending": 1}
    assert summary.title == "[bold cyan]Overall Progress[/bold cyan]"
    assert errors is not None and "Recent Errors" in str(errors.title)
    assert layout is not None

    updates = []

    class FakeLive:
        def __init__(self, layout, console, refresh_per_second):
            self.layout = layout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def update(self, value):
            updates.append(value)

    monkeypatch.setattr("aap_migration.reporting.enhanced_progress.Live", FakeLive)
    result = display.run_with_live_display(lambda: "done")
    display.update_display()

    assert result == "done"
    assert len(updates) >= 2
