from __future__ import annotations

from datetime import UTC, datetime

from aap_migration.analysis.dependency_analyzer import (
    GlobalDependencyReport,
    OrgDependencyReport,
    ResourceDependency,
)
from aap_migration.analysis.html_report import generate_html_report


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
            {
                "phase": 1,
                "description": "Independent",
                "orgs": ["SharedOrg"],
                "has_cycle": False,
                "cycles": [],
            },
            {
                "phase": 2,
                "description": "Dependent",
                "orgs": ["DependentOrg"],
                "has_cycle": False,
                "cycles": [],
            },
        ],
    )


def test_dependency_html_report_escapes_and_embeds_data() -> None:
    html = generate_html_report(build_global_report())

    assert "AAP Migration — Dependency Analysis" in html
    assert '"name": "Shared & <Danger>"' in html
    assert '"name": "JT <One>"' in html
    assert '"name": "SharedOrg"' in html
    assert '"name": "DependentOrg"' in html
    assert '"description": "Independent"' in html
    assert '"description": "Dependent"' in html
