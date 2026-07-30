from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from aap_migration.cli.commands import migration_report as report_module
from aap_migration.migration.models import IDMapping, MigrationProgress


def _write_batches(base: Path, resource_type: str, items: list[dict]) -> None:
    subdir = base / resource_type
    subdir.mkdir(parents=True, exist_ok=True)
    (subdir / f"{resource_type}_001.json").write_text(json.dumps(items))


def _add_progress(
    db_session,
    resource_type: str,
    source_id: int,
    name: str,
    status: str,
    phase: str,
    error: str | None = None,
) -> MigrationProgress:
    record = MigrationProgress(
        resource_type=resource_type,
        source_id=source_id,
        source_name=name,
        status=status,
        phase=phase,
        error_message=error,
    )
    db_session.add(record)
    db_session.flush()
    return record


def test_analyze_resource_type_counts_failures_and_missing_resources(
    db_session,
    sqlite_db_url: str,
    tmp_path: Path,
) -> None:
    export_dir = tmp_path / "exports"
    transform_dir = tmp_path / "xformed"
    export_dir.mkdir()
    transform_dir.mkdir()

    (export_dir / "teams.json").write_text(json.dumps([{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]))
    (transform_dir / "teams.json").write_text(
        json.dumps(
            [
                {"id": 1, "name": "A"},
                {"id": 2, "name": "B"},
                {"id": 3, "name": "C"},
                {"id": 4, "name": "D"},
            ]
        )
    )

    _add_progress(db_session, "teams", 1, "A", "completed", "import")
    _add_progress(db_session, "teams", 2, "B", "failed", "export", "export boom")
    _add_progress(db_session, "teams", 3, "C", "skipped", "transform", "needs org")
    _add_progress(db_session, "teams", 5, "E", "pending", "import")
    _add_progress(db_session, "teams", 6, "F", "in_progress", "import")
    db_session.commit()

    stats = report_module._analyze_resource_type("teams", export_dir, transform_dir, sqlite_db_url)

    assert stats["exported_count"] == 4
    assert stats["transformed_count"] == 4
    assert stats["completed_count"] == 1
    assert stats["failed_count"] == 1
    assert stats["pending_count"] == 1
    assert stats["in_progress_count"] == 1
    assert stats["skipped_count"] == 1
    assert stats["export_failed"][0]["source_id"] == 2
    assert stats["transform_skipped"][0]["source_id"] == 3
    assert stats["discrepancy"] == 1
    assert {item["source_id"] for item in stats["missing_resources"]} == {2, 3, 4}


def test_format_workflow_nodes_failures_groups_job_template_statuses(
    db_session,
    sqlite_db_url: str,
) -> None:
    _add_progress(db_session, "job_templates", 33, "JT skipped", "skipped", "import")
    _add_progress(db_session, "job_templates", 34, "JT failed", "failed", "import")
    _add_progress(db_session, "job_templates", 35, "JT ok", "completed", "import")
    db_session.commit()

    migration_state = SimpleNamespace(database_url=sqlite_db_url)
    lines = report_module._format_workflow_nodes_failures(
        [
            {
                "source_id": 100,
                "error": "Referenced job template (source_id=33) was not successfully imported",
            },
            {
                "source_id": 101,
                "error": "Referenced job template (source_id=34) was not successfully imported",
            },
            {
                "source_id": 102,
                "error": "Referenced job template (source_id=35) was not successfully imported",
            },
            {"source_id": 103, "error": "workflow node without job template reference"},
        ],
        migration_state,
    )
    text = "\n".join(lines)

    assert "JT skipped" in text
    assert "(skipped - already exists in target)" in text
    assert "JT failed" in text
    assert "(failed to import)" in text
    assert "JT ok" in text
    assert "(successfully imported)" in text
    assert "(not found in migration)" in text
    assert "Ensure all referenced job templates are successfully imported first" in text


def test_generate_markdown_report_and_command_callback(
    db_session,
    sqlite_db_url: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    export_dir = tmp_path / "exports"
    transform_dir = tmp_path / "xformed"
    report_dir = tmp_path / "reports"
    export_dir.mkdir()
    transform_dir.mkdir()
    report_dir.mkdir()

    _write_batches(export_dir, "projects", [{"id": 1, "name": "ProjA"}, {"id": 2, "name": "ProjB"}])
    _write_batches(
        transform_dir, "projects", [{"id": 1, "name": "ProjA"}, {"id": 2, "name": "ProjB"}]
    )
    _write_batches(export_dir, "users", [{"id": 10, "name": "UserA"}, {"id": 11, "name": "UserB"}])
    _write_batches(
        transform_dir, "users", [{"id": 10, "name": "UserA"}, {"id": 11, "name": "UserB"}]
    )
    _write_batches(
        export_dir,
        "credentials",
        [{"id": 70, "name": "CredA"}, {"id": 71, "name": "CredB"}, {"id": 72, "name": "CredC"}],
    )
    _write_batches(
        transform_dir,
        "credentials",
        [{"id": 70, "name": "CredA"}, {"id": 71, "name": "CredB"}, {"id": 72, "name": "CredC"}],
    )
    _write_batches(export_dir, "workflow_job_templates", [{"id": 60, "name": "WF A"}])
    _write_batches(transform_dir, "workflow_job_templates", [{"id": 60, "name": "WF A"}])
    _write_batches(export_dir, "workflow_nodes", [{"id": 50, "name": "Node A"}])
    _write_batches(transform_dir, "workflow_nodes", [{"id": 50, "name": "Node A"}])
    _write_batches(export_dir, "inventories", [{"id": 80, "name": "Inv"}])
    _write_batches(export_dir, "schedules", [{"id": 90, "name": "Nightly"}])

    project_warning = _add_progress(
        db_session,
        "projects",
        1,
        "ProjA",
        "completed",
        "import",
        "WARNING: partial association",
    )
    _add_progress(db_session, "projects", 2, "ProjB", "failed", "import", "bad | pipe")
    _add_progress(db_session, "users", 10, "UserA", "completed", "import")
    _add_progress(db_session, "credentials", 70, "CredA", "completed", "import")
    _add_progress(db_session, "credentials", 71, "CredB", "completed", "import")
    _add_progress(db_session, "workflow_job_templates", 60, "WF A", "completed", "import")
    _add_progress(
        db_session, "workflow_job_templates", 61, "WF B", "failed", "import", "node problem"
    )
    _add_progress(
        db_session,
        "workflow_nodes",
        50,
        "Node A",
        "failed",
        "import",
        "Referenced job template (source_id=33) was not successfully imported",
    )
    _add_progress(db_session, "job_templates", 33, "JT one", "skipped", "import")
    _add_progress(db_session, "inventories", 80, "Inv", "failed", "export", "source denied")
    _add_progress(
        db_session, "schedules", 90, "Nightly", "skipped", "transform", "missing dependency"
    )

    db_session.add_all(
        [
            IDMapping(
                resource_type="credentials",
                source_id=70,
                target_id=900,
                source_name="CredA",
                migration_progress_id=project_warning.id,
            ),
            IDMapping(
                resource_type="credentials",
                source_id=71,
                target_id=900,
                source_name="CredB",
                migration_progress_id=project_warning.id,
            ),
        ]
    )
    db_session.commit()

    migration_state = SimpleNamespace(database_url=sqlite_db_url, migration_id="mig-123")

    report_data = [
        report_module._analyze_resource_type("projects", export_dir, transform_dir, sqlite_db_url),
        report_module._analyze_resource_type("users", export_dir, transform_dir, sqlite_db_url),
        report_module._analyze_resource_type(
            "credentials", export_dir, transform_dir, sqlite_db_url
        ),
        report_module._analyze_resource_type(
            "workflow_job_templates", export_dir, transform_dir, sqlite_db_url
        ),
        report_module._analyze_resource_type(
            "workflow_nodes", export_dir, transform_dir, sqlite_db_url
        ),
        report_module._analyze_resource_type(
            "inventories", export_dir, transform_dir, sqlite_db_url
        ),
        report_module._analyze_resource_type("schedules", export_dir, transform_dir, sqlite_db_url),
    ]

    markdown = report_module._generate_markdown_report(report_data, migration_state)
    assert "## Workflow Job Templates - Node Import Status" in markdown
    assert "## Export Phase Issues" in markdown
    assert "## Transform Phase Issues" in markdown
    assert "### Warnings (1)" in markdown
    assert "### ⚠️ CRITICAL: Duplicate Target Mappings Detected" in markdown
    assert "### Missing Resources (Discrepancy: 1)" in markdown
    assert "workflow_nodes" in markdown

    summary_calls = []
    info_calls = []
    success_calls = []
    monkeypatch.setattr(report_module, "_print_summary", lambda data: summary_calls.append(data))
    monkeypatch.setattr(report_module, "echo_info", lambda msg: info_calls.append(msg))
    monkeypatch.setattr(report_module, "echo_success", lambda msg: success_calls.append(msg))

    callback = report_module.generate_migration_report.callback
    while hasattr(callback, "__wrapped__"):
        callback = callback.__wrapped__

    ctx = SimpleNamespace(
        config=SimpleNamespace(
            paths=SimpleNamespace(
                report_dir=str(report_dir),
                export_dir=str(export_dir),
                transform_dir=str(transform_dir),
            )
        ),
        migration_state=migration_state,
    )
    callback(ctx, None, None)

    output_path = report_dir / "migration-report.md"
    assert output_path.exists()
    generated = output_path.read_text()
    assert "Migration ID:** mig-123" in generated
    assert "CRITICAL: Duplicate Target Mappings Detected" in generated
    assert info_calls == ["Generating migration report..."]
    assert success_calls == [f"Migration report generated: {output_path}"]
    assert len(summary_calls) == 1


def test_print_summary_emits_status_lines(capsys) -> None:
    report_module._print_summary(
        [
            {
                "resource_type": "projects",
                "exported_count": 2,
                "completed_count": 2,
                "failed_count": 0,
                "skipped_count": 0,
                "discrepancy": 0,
            },
            {
                "resource_type": "users",
                "exported_count": 2,
                "completed_count": 1,
                "failed_count": 0,
                "skipped_count": 1,
                "discrepancy": 0,
            },
            {
                "resource_type": "teams",
                "exported_count": 2,
                "completed_count": 1,
                "failed_count": 1,
                "skipped_count": 0,
                "discrepancy": 1,
            },
        ]
    )

    output = capsys.readouterr().out
    assert "MIGRATION SUMMARY" in output
    assert "projects" in output
    assert "users" in output
    assert "teams" in output
