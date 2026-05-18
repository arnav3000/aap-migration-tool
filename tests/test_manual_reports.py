from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Console

from aap_migration.analysis.dependency_analyzer import (
    GlobalDependencyReport,
    OrgDependencyReport,
    ResourceDependency,
)
from aap_migration.analysis.html_report import generate_html_report
from aap_migration.reporting.migration_report import MigrationReport
from aap_migration.reporting.schema_report import (
    display_detailed_comparison,
    display_schema_comparison_summary,
    generate_schema_report_text,
    save_schema_report,
)
from aap_migration.schema.models import (
    ChangeType,
    ComparisonResult,
    FieldDiff,
    SchemaChange,
    Severity,
)


def build_global_report() -> GlobalDependencyReport:
    shared_project = ResourceDependency("projects", 8, "Shared & <Danger>", "SharedOrg")
    shared_project.add_usage("job_templates", 12, "JT <One>")

    dependent = OrgDependencyReport(
        org_name="DependentOrg",
        org_id=2,
        resource_count=3,
        has_cross_org_deps=True,
        dependencies={"SharedOrg": [shared_project]},
        can_migrate_standalone=False,
        required_migrations_before=["SharedOrg"],
        resources={
            "projects": [{"id": 8, "name": "Shared & <Danger>"}],
            "job_templates": [{"id": 12, "name": "JT <One>"}],
            "hosts": ["ignore-non-dict"],
        },
    )
    independent = OrgDependencyReport(
        org_name="SharedOrg",
        org_id=1,
        resource_count=1,
        has_cross_org_deps=False,
        dependencies={},
        can_migrate_standalone=True,
        required_migrations_before=[],
        resources={"projects": [{"id": 8, "name": "Shared & <Danger>"}]},
    )
    return GlobalDependencyReport(
        analysis_date=datetime(2026, 1, 1, tzinfo=UTC),
        source_url="https://source.example.com",
        total_organizations=2,
        analyzed_organizations=["SharedOrg", "DependentOrg"],
        independent_orgs=["SharedOrg"],
        dependent_orgs=["DependentOrg"],
        org_reports={"SharedOrg": independent, "DependentOrg": dependent},
        migration_order=["SharedOrg", "DependentOrg"],
        migration_phases=[
            ["SharedOrg"],
            {"phase": 2, "description": "Dependent", "orgs": ["DependentOrg"]},
        ],
    )


def test_manual_migration_report_formats_and_helpers(tmp_path) -> None:
    report = MigrationReport(source_url="https://source", target_url="https://target")
    report.successful_imports = {"projects": 2, "users": 1}
    report.add_failed_import("projects", 7, "Project A", "ConflictError", "already exists" * 10)
    report.add_unresolved_dependency(
        "job_templates",
        "Deploy",
        9,
        "project",
        "projects",
        7,
        "missing mapping",
    )
    report.add_encrypted_credential(11, "Machine Cred", 1, 5, ["password"], "Create manually")

    json_text = report.to_json()
    md_text = report.to_markdown()
    rows = report.to_csv_rows()
    summary = report.get_summary_dict()

    assert '"failed_imports"' in json_text
    assert "## Credentials Requiring Manual Creation" in md_text
    assert "Create manually" in md_text
    assert [row["category"] for row in rows] == [
        "failed_import",
        "unresolved_dependency",
        "encrypted_credential",
    ]
    assert summary["failed_imports_count"] == 1
    assert summary["unresolved_dependencies_count"] == 1
    assert summary["encrypted_credentials_count"] == 1
    assert report.has_issues() is True

    json_path = tmp_path / "report.json"
    md_path = tmp_path / "report.md"
    csv_path = tmp_path / "report.csv"
    report.save_json(json_path)
    report.save_markdown(md_path)
    report.save_csv(csv_path)
    assert json_path.read_text() == json_text
    assert "Machine Cred" in md_path.read_text()
    assert "encrypted_credential" in csv_path.read_text()

    empty_csv_path = tmp_path / "empty.csv"
    MigrationReport().save_csv(empty_csv_path)
    assert (
        "category,resource_type,name,source_id,error,action_required" in empty_csv_path.read_text()
    )


def test_schema_report_renderers_and_save(tmp_path) -> None:
    unchanged = ComparisonResult(resource_type="users", source_schema={}, target_schema={})
    changed = ComparisonResult(
        resource_type="projects",
        source_schema={},
        target_schema={},
        field_diffs=[
            FieldDiff(
                field_name="scm_branch",
                change_type=ChangeType.FIELD_REMOVED,
                severity=Severity.HIGH,
                description="Removed field",
                recommendation="Map to prompt",
            ),
            FieldDiff(
                field_name="default_environment",
                change_type=ChangeType.FIELD_ADDED,
                severity=Severity.MEDIUM,
                target_value={"required": True, "default": "prod"},
                description="New required field",
                recommendation="Set default",
            ),
        ],
        schema_changes=[
            SchemaChange(
                resource_type="projects",
                change_type=ChangeType.VALIDATION_CHANGED,
                severity=Severity.CRITICAL,
                description="Validation stricter",
                recommendation="Review inputs",
            )
        ],
    )

    console = Console(record=True, width=140)
    display_schema_comparison_summary({"users": unchanged, "projects": changed}, console=console)
    display_detailed_comparison(unchanged, console=console)
    display_detailed_comparison(changed, console=console)
    output = console.export_text()

    assert "Source → Target Schema Comparison Summary" in output
    assert "projects" in output
    assert "BREAKING CHANGES DETECTED" in output
    assert "Validation Changes: projects" in output

    text_report = generate_schema_report_text({"users": unchanged, "projects": changed})
    assert "Resource types with changes: 1" in text_report
    assert "WARNING: Breaking changes detected!" in text_report
    assert "Validation Changes:" in text_report

    out_path = tmp_path / "schema.txt"
    save_schema_report({"projects": changed}, str(out_path))
    assert "PROJECTS" in out_path.read_text()


def test_dependency_html_report_escapes_and_embeds_data() -> None:
    html = generate_html_report(build_global_report())

    assert "AAP Migration Mind Map - Dependency Analysis" in html
    assert "Shared &amp; &lt;Danger&gt;" in html
    assert "JT &lt;One&gt;" in html
    assert '"from": "SharedOrg"' in html
    assert '"to": "DependentOrg"' in html
    assert '"description": "Phase 1"' in html
    assert '"description": "Dependent"' in html
