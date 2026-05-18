from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from aap_migration.analysis.dependency_analyzer import (
    GlobalDependencyReport,
    OrgDependencyReport,
    ResourceDependency,
)
from aap_migration.analysis.reports import (
    format_detailed_report as format_detailed_report_v1,
)
from aap_migration.analysis.reports import (
    format_summary_report as format_summary_report_v1,
)
from aap_migration.analysis.text_report import (
    format_detailed_report as format_detailed_report_v2,
)
from aap_migration.analysis.text_report import (
    format_summary_report as format_summary_report_v2,
)
from aap_migration.cli.commands.config import (
    _display_config_summary,
    _test_connectivity,
    _validate_paths,
    _validate_settings,
)
from aap_migration.cli.context import MigrationContext
from aap_migration.cli.decorators import (
    confirm_action,
    handle_errors,
    pass_context,
    requires_config,
)
from aap_migration.cli.utils import (
    confirm_overwrite,
    create_progress_bar,
    echo_error,
    echo_info,
    echo_step_complete,
    echo_step_pending,
    echo_step_running,
    echo_success,
    echo_warning,
    format_count,
    format_duration,
    format_timestamp,
    load_json_or_yaml,
    print_stats,
    print_table,
    step_progress,
    validate_path,
)
from aap_migration.client.exceptions import (
    APIError,
    AuthenticationError,
    ConfigurationError,
    StateError,
)
from aap_migration.reporting.report import MigrationReport, generate_migration_report


def build_dependency_report() -> tuple[
    GlobalDependencyReport, OrgDependencyReport, OrgDependencyReport
]:
    shared_project = ResourceDependency(
        "projects", 10, "VeryLongSharedProjectNameForTruncationChecks", "SharedOrg"
    )
    shared_project.add_usage("job_templates", 101, "Template using shared project")

    isolated = OrgDependencyReport(
        org_name="IsolatedOrg",
        org_id=1,
        resource_count=4,
        has_cross_org_deps=False,
        dependencies={},
        can_migrate_standalone=True,
        required_migrations_before=[],
    )
    dependent = OrgDependencyReport(
        org_name="DependentOrg",
        org_id=2,
        resource_count=6,
        has_cross_org_deps=True,
        dependencies={"SharedOrg": [shared_project]},
        can_migrate_standalone=False,
        required_migrations_before=["SharedOrg"],
    )
    report = GlobalDependencyReport(
        analysis_date=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        source_url="https://source.example.com",
        total_organizations=2,
        analyzed_organizations=["IsolatedOrg", "DependentOrg"],
        independent_orgs=["IsolatedOrg"],
        dependent_orgs=["DependentOrg"],
        org_reports={"IsolatedOrg": isolated, "DependentOrg": dependent},
        migration_order=["IsolatedOrg", "DependentOrg"],
        migration_phases=[
            {"phase": 1, "orgs": ["IsolatedOrg"], "description": "Start isolated orgs"}
        ],
    )
    return report, isolated, dependent


def fake_config(tmp_path: Path):
    return SimpleNamespace(
        source=SimpleNamespace(url="https://source.example.com", token="source", verify_ssl=True),
        target=SimpleNamespace(url="https://target.example.com", token="target", verify_ssl=False),
        state=SimpleNamespace(db_path=str(tmp_path / "state" / "migration.db")),
        performance=SimpleNamespace(
            batch_sizes={"default": 100, "hosts": 201},
            max_concurrent=51,
            rate_limit=10,
            http_max_connections=20,
            http_max_keepalive_connections=10,
        ),
        logging=SimpleNamespace(log_payloads=False, max_payload_size=4096),
    )


def test_analysis_report_formatters_cover_summary_and_detail() -> None:
    report, isolated, dependent = build_dependency_report()

    for formatter in (format_summary_report_v1, format_summary_report_v2):
        summary = formatter(report)
        assert "Cross-Organization Dependency Analysis" in summary
        assert "Independent Organizations" in summary
        assert "Organizations with Cross-Org Dependencies" in summary
        assert "Phase 1: Start isolated orgs" in summary
        assert "DependentOrg (needs: SharedOrg)" in summary

    for formatter in (format_detailed_report_v1, format_detailed_report_v2):
        standalone = formatter(isolated)
        detailed = formatter(dependent)
        assert "Can be migrated standalone" in standalone
        assert 'aap-bridge migrate -o "IsolatedOrg"' in standalone
        assert "Cross-Organization Dependencies" in detailed
        assert "Depends on: SharedOrg" in detailed
        assert "Required by:" in detailed
        assert '1. aap-bridge migrate -o "SharedOrg"' in detailed


def test_migration_report_generators_and_output_files(tmp_path: Path) -> None:
    summary = {
        "status": "completed",
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T00:05:00Z",
        "duration_seconds": 305,
        "dry_run": True,
        "phases_completed": 3,
        "phases_failed": 1,
        "total_resources_exported": 20,
        "total_resources_imported": 16,
        "total_resources_failed": 2,
        "total_resources_skipped": 2,
        "errors": [{"phase": "import", "error": "boom", "timestamp": "now"}] * 11,
        "skipped_items": [
            {"phase": "import", "resource_type": "credentials", "name": "cred1", "reason": "exists"}
        ]
        * 11,
    }
    report = MigrationReport("mig-123", summary)

    json_path = tmp_path / "report.json"
    md_path = tmp_path / "report.md"
    html_path = tmp_path / "report.html"

    json_text = report.generate_json(str(json_path))
    md_text = report.generate_markdown(str(md_path))
    html_text = report.generate_html(str(html_path))

    assert '"migration_id": "mig-123"' in json_text
    assert json_path.exists()
    assert "## Errors" in md_text
    assert "... and 1 more errors" in md_text
    assert "## Recommendations" in md_text
    assert md_path.exists()
    assert '<span class="status success">completed</span>' in html_text
    assert "Skipped Items (11)" in html_text
    assert html_path.exists()

    assert report._generate_statistics()["success_rate"] == 80.0
    recommendations = report._generate_recommendations()
    assert any("failed to migrate" in rec for rec in recommendations)
    assert any("below 95%" in rec for rec in recommendations)
    assert any("dry run" in rec.lower() for rec in recommendations)
    assert report._format_duration(None) == "N/A"
    assert report._format_duration(59) == "59s"
    assert report._format_duration(125) == "2m 5s"
    assert report._format_duration(3661) == "1h 1m 1s"

    generated = generate_migration_report("mig-123", summary, output_dir=str(tmp_path / "bundle"))
    assert set(generated) == {"json", "markdown", "html"}
    assert all(Path(path).exists() for path in generated.values())

    success_report = MigrationReport(
        "mig-ok",
        {"status": "completed", "total_resources_exported": 5, "total_resources_imported": 5},
    )
    assert success_report._generate_recommendations() == [
        "✅ Migration completed successfully! All resources migrated with high success rate."
    ]


def test_cli_context_and_config_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = fake_config(tmp_path)
    load_calls = []

    monkeypatch.setattr(
        "aap_migration.cli.context.load_config_from_yaml",
        lambda path: load_calls.append(path) or config,
    )

    created = []

    class FakeSourceClient:
        def __init__(self, **kwargs):
            created.append(("source", kwargs))

    class FakeTargetClient:
        def __init__(self, **kwargs):
            created.append(("target", kwargs))

    class FakeState:
        def __init__(self, config):
            created.append(("state", config))

    monkeypatch.setattr("aap_migration.cli.context.AAPSourceClient", FakeSourceClient)
    monkeypatch.setattr("aap_migration.cli.context.AAPTargetClient", FakeTargetClient)
    monkeypatch.setattr("aap_migration.cli.context.MigrationState", FakeState)

    ctx = MigrationContext(config_path=tmp_path / "config.yaml")
    assert ctx.config is config
    assert ctx.config is config
    assert load_calls == [tmp_path / "config.yaml"]
    assert isinstance(ctx.source_client, FakeSourceClient)
    assert isinstance(ctx.target_client, FakeTargetClient)
    assert isinstance(ctx.migration_state, FakeState)

    with MigrationContext(config_path=tmp_path / "config.yaml") as context_manager:
        monkeypatch.setattr(
            "aap_migration.cli.context.load_config_from_yaml",
            lambda path: config,
        )
        _ = context_manager.config
    ctx.cleanup()

    printed = []
    monkeypatch.setattr(
        "aap_migration.cli.commands.config.print_table",
        lambda title, columns, rows: printed.append((title, columns, rows)),
    )
    messages = []
    monkeypatch.setattr(
        "aap_migration.cli.commands.config.echo_success",
        lambda message: messages.append(("success", message)),
    )
    monkeypatch.setattr(
        "aap_migration.cli.commands.config.echo_warning",
        lambda message: messages.append(("warning", message)),
    )
    monkeypatch.setattr(
        "aap_migration.cli.commands.config.echo_error",
        lambda message: messages.append(("error", message)),
    )
    monkeypatch.setattr(
        "aap_migration.cli.commands.config.echo_info",
        lambda message: messages.append(("info", message)),
    )

    _display_config_summary(config)
    _validate_paths(config)
    _validate_settings(config)
    assert printed[0][0] == "Configuration Summary"
    assert ("warning", "Host batch size (201) exceeds recommended maximum (200)") in messages
    assert any("High concurrency" in message for kind, message in messages if kind == "warning")

    config.performance.max_concurrent = 0
    with pytest.raises(click.ClickException, match="Max concurrent requests must be positive"):
        _validate_settings(config)
    config.performance.max_concurrent = 10
    config.performance.rate_limit = 0
    with pytest.raises(click.ClickException, match="Rate limit must be positive"):
        _validate_settings(config)

    config.performance.rate_limit = 10
    connectivity_ctx = SimpleNamespace(
        config=config, source_client=object(), target_client=object()
    )
    _test_connectivity(connectivity_ctx)


def test_cli_utils_and_decorators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secho_calls = []
    monkeypatch.setattr(
        "click.secho", lambda message, **kwargs: secho_calls.append((message, kwargs))
    )
    echo_success("ok")
    echo_error("bad")
    echo_warning("warn")
    echo_info("info")
    echo_step_complete("done")
    echo_step_running("run")
    echo_step_pending("wait")
    assert [call[0] for call in secho_calls] == [
        "✓ ok",
        "✗ bad",
        "⚠ warn",
        "ℹ info",
        "✓ done",
        ":: run",
        "• wait",
    ]

    printed = []
    monkeypatch.setattr(
        "aap_migration.cli.utils.console.print", lambda message: printed.append(message)
    )

    class FakeStatus:
        def __init__(self, *args, **kwargs):
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    monkeypatch.setattr("rich.status.Status", FakeStatus)
    with step_progress("Testing"):
        pass
    with pytest.raises(RuntimeError):
        with step_progress("Failing"):
            raise RuntimeError("boom")
    assert printed == ["[green]✓[/green] Testing", "[red]✗[/red] Failing"]

    assert format_duration(12.34) == "12.3s"
    assert format_duration(75) == "1m 15s"
    assert format_duration(3670) == "1h 1m 10s"
    assert format_timestamp(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02 03:04:05"
    assert format_count(1234567) == "1,234,567"

    tables = []
    monkeypatch.setattr("aap_migration.cli.utils.console.print", lambda value: tables.append(value))
    print_table("Demo", ["A", "B"], [[1, 2]])
    print_stats({"resources_imported": 5})
    assert len(tables) == 2
    assert create_progress_bar("Demo").console is not None

    existing_dir = tmp_path / "dir"
    existing_dir.mkdir()
    existing_file = tmp_path / "data.json"
    existing_file.write_text('{"x": 1}')
    yaml_file = tmp_path / "data.yaml"
    yaml_file.write_text("x: 2\n")

    validate_path(existing_file, must_exist=True, must_be_file=True)
    validate_path(existing_dir, must_exist=True, must_be_dir=True)
    with pytest.raises(click.BadParameter, match="Path does not exist"):
        validate_path(tmp_path / "missing.txt", must_exist=True)
    with pytest.raises(click.BadParameter, match="Path is not a file"):
        validate_path(existing_dir, must_be_file=True)
    with pytest.raises(click.BadParameter, match="Path is not a directory"):
        validate_path(existing_file, must_be_dir=True)

    monkeypatch.setattr("click.confirm", lambda message: False)
    assert confirm_overwrite(tmp_path / "new.json") is True
    assert confirm_overwrite(existing_file, force=True) is True
    assert confirm_overwrite(existing_file, force=False) is False
    assert load_json_or_yaml(existing_file) == {"x": 1}
    assert load_json_or_yaml(yaml_file) == {"x": 2}
    with pytest.raises(click.BadParameter, match="Unsupported file format"):
        load_json_or_yaml(tmp_path / "notes.txt")

    runner = CliRunner()

    @click.command()
    @pass_context
    def show_ctx(ctx):
        click.echo(str(ctx.config_path))

    result = runner.invoke(show_ctx, obj=MigrationContext(config_path=Path("config.yaml")))
    assert result.exit_code == 0
    assert "config.yaml" in result.output

    calls = []

    @requires_config
    def needs_config(ctx):
        calls.append("ran")

    with pytest.raises(click.exceptions.Exit) as missing_exit:
        needs_config(MigrationContext())
    assert missing_exit.value.exit_code == 2

    good_ctx = MigrationContext(config_path=Path("config.yaml"))
    good_ctx._config = SimpleNamespace()
    needs_config(good_ctx)
    assert calls == ["ran"]

    @confirm_action("Proceed?", "Cancelled")
    def guarded():
        return "ok"

    monkeypatch.setattr("click.get_current_context", lambda: SimpleNamespace(params={"yes": True}))
    assert guarded() == "ok"
    monkeypatch.setattr("click.get_current_context", lambda: SimpleNamespace(params={"yes": False}))
    monkeypatch.setattr("click.confirm", lambda message: False)
    with pytest.raises(click.exceptions.Exit) as abort_exit:
        guarded()
    assert abort_exit.value.exit_code == 0

    echoed = []
    monkeypatch.setattr("click.echo", lambda message, err=False: echoed.append((message, err)))

    @handle_errors
    def raise_config():
        raise ConfigurationError("bad config")

    @handle_errors
    def raise_auth():
        raise AuthenticationError("bad auth")

    @handle_errors
    def raise_api():
        raise APIError("broken", status_code=503)

    @handle_errors
    def raise_state():
        raise StateError("db issue")

    @handle_errors
    def raise_other():
        raise RuntimeError("oops")

    for func, code in [
        (raise_config, 2),
        (raise_auth, 3),
        (raise_api, 4),
        (raise_state, 5),
        (raise_other, 1),
    ]:
        with pytest.raises(click.exceptions.Exit) as exc:
            func()
        assert exc.value.exit_code == code

    assert any("Configuration Error" in message for message, _ in echoed)
    assert any("Authentication Error" in message for message, _ in echoed)
    assert any("API Error" in message for message, _ in echoed)
    assert any("State Error" in message for message, _ in echoed)
    assert any("Unexpected Error" in message for message, _ in echoed)
