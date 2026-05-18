from __future__ import annotations

from datetime import UTC, datetime

from aap_migration.analysis.dependency_analyzer import (
    GlobalDependencyReport,
    OrgDependencyReport,
    ResourceDependency,
)
from aap_migration.analysis.dependency_graph import (
    _partial_topological_sort,
    detect_cycles,
    group_into_phases,
    group_into_phases_with_cycles,
    topological_sort,
)
from aap_migration.analysis.quality import (
    DuplicateResource,
    NamingPattern,
    QualityReport,
    analyze_naming_patterns,
    calculate_quality_score,
    detect_case_style,
    detect_duplicates,
    detect_prefix,
    generate_quality_report,
)


def test_dependency_graph_orders_detects_cycles_and_groups_phases() -> None:
    graph = {
        "Default": ["Engineering"],
        "Engineering": [],
        "DevOps": ["Engineering", "Default"],
    }

    order = topological_sort(graph)
    partial, remaining = _partial_topological_sort({"A": ["B"], "B": ["A"], "C": ["A"]})

    assert order == ["Engineering", "Default", "DevOps"]
    assert group_into_phases(graph, order) == [
        {
            "phase": 1,
            "orgs": ["Engineering"],
            "description": "Independent organizations (no dependencies)",
        },
        {
            "phase": 2,
            "orgs": ["Default"],
            "description": "Organizations dependent on Phase 1 migrations",
        },
        {
            "phase": 3,
            "orgs": ["DevOps"],
            "description": "Organizations dependent on Phase 2 migrations",
        },
    ]
    assert partial == []
    assert remaining == {"A", "B", "C"}
    assert detect_cycles({"A": ["B"], "B": ["A"], "C": ["A"]}) == [["A", "B"]]

    with_cycles_order, with_cycles_phases = group_into_phases_with_cycles(
        {"A": ["B"], "B": ["A"], "C": ["A"], "D": []}
    )
    assert with_cycles_order == ["D", "A", "B", "C"]
    assert with_cycles_phases[0]["orgs"] == ["D"]
    assert with_cycles_phases[1]["orgs"] == ["A", "B"]
    assert with_cycles_phases[2]["orgs"] == ["C"]


def test_dependency_graph_raises_for_unresolvable_cycle() -> None:
    try:
        topological_sort({"A": ["B"], "B": ["A"]})
    except ValueError as exc:
        assert "Circular dependencies detected" in str(exc)
    else:
        raise AssertionError("Expected topological_sort to fail for a cycle")


def test_quality_helpers_detect_duplicates_patterns_and_summaries() -> None:
    resources = {
        "job_templates": [
            {"id": 1, "name": "prod-deploy"},
            {"id": 2, "name": "prod-deploy"},
            {"id": 3, "name": "ProdDeploy"},
        ],
        "inventories": [
            {"id": 4, "name": "prod-web"},
            {"id": 5, "name": "prod-web"},
            {"id": 6, "name": "DEV_WEB"},
        ],
        "credentials": [
            {"id": 7, "name": "Vault", "credential_type": "Machine"},
            {"id": 8, "name": "Vault", "credential_type": "Machine"},
            {"id": 9, "name": "Vault", "credential_type": "Vault"},
        ],
        "projects": [
            {"id": 10, "name": "eng-app"},
        ],
    }

    duplicates = detect_duplicates(resources, "Default")
    naming = analyze_naming_patterns(resources)
    report = generate_quality_report(resources, "Default")

    assert any(d.name == "prod-deploy" and d.severity == "warning" for d in duplicates)
    assert any(d.name == "Vault" and d.count == 2 for d in duplicates)
    assert calculate_quality_score(duplicates) < 100.0
    assert detect_case_style("prod-app") == "kebab-case"
    assert detect_case_style("DEV_APP") == "UPPER_CASE"
    assert detect_case_style("ProdApp") == "PascalCase"
    assert detect_prefix("eng-app") == "team:eng-"
    assert detect_prefix("alpha_app") == "custom:alpha-"
    assert naming.total_resources == 7
    assert naming.dominant_pattern
    assert report.duplicate_count == len(duplicates)
    assert report.naming_pattern is not None
    assert report.get_severity_counts()["warning"] >= 1
    assert "job_templates" in report.get_duplicates_by_type()


def test_quality_dataclasses_and_global_dependency_summary() -> None:
    duplicate = DuplicateResource(
        name="Deploy",
        resource_type="job_templates",
        count=3,
        ids=[1, 2, 3],
        severity="error",
        impact="high",
        recommendation="rename them",
    )
    pattern = NamingPattern(
        case_style={"kebab-case": 2}, prefixes={"env:prod-": 2}, total_resources=2
    )
    quality = QualityReport(
        org_name="Default", duplicate_count=1, duplicates=[duplicate], quality_score=88.5
    )
    dependency = ResourceDependency("credentials", 7, "Vault", "Engineering")
    dependency.add_usage("job_template", 99, "Deploy")
    org_report = OrgDependencyReport(
        org_name="Default",
        org_id=1,
        resource_count=10,
        has_cross_org_deps=True,
        dependencies={"Engineering": [dependency]},
        can_migrate_standalone=False,
        required_migrations_before=["Engineering"],
        quality_report=quality,
    )
    global_report = GlobalDependencyReport(
        analysis_date=datetime.now(UTC),
        source_url="https://source.example.com",
        total_organizations=1,
        analyzed_organizations=["Default"],
        independent_orgs=[],
        dependent_orgs=["Default"],
        org_reports={"Default": org_report},
        migration_order=["Engineering", "Default"],
        migration_phases=[{"phase": 1, "orgs": ["Engineering"]}],
    )

    assert duplicate.severity_emoji == "🔴"
    assert duplicate.resource_type_display == "Job Template"
    assert pattern.get_case_distribution_percent() == {"kebab-case": 100.0}
    assert quality.get_duplicates_by_type()["job_templates"][0].name == "Deploy"
    assert dependency.required_by == [{"type": "job_template", "id": 99, "name": "Deploy"}]
    assert org_report.get_total_cross_org_resources() == 1
    assert global_report.get_quality_summary() == {
        "total_duplicates": 1,
        "total_errors": 1,
        "total_warnings": 0,
        "average_quality_score": 88.5,
        "orgs_analyzed": 1,
    }
