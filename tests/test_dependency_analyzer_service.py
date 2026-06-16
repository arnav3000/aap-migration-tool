from __future__ import annotations

import pytest

import aap_migration.api.models as api_models
from aap_migration.analysis.dependency_analyzer import (
    CrossOrgDependencyAnalyzer,
    OrgDependencyReport,
    ResourceDependency,
)

api_models.Job = api_models.JobRecord

from aap_migration.api.services.analysis_service import _serialize_report  # noqa: E402


class FakeClient:
    def __init__(self):
        self.base_url = "https://source.example.com"

    async def get_paginated(self, endpoint: str, params=None):
        params = params or {}
        if endpoint == "organizations/" and params.get("name") == "OrgA":
            return [{"id": 1, "name": "OrgA", "modified": "2026-01-01T00:00:00"}]
        if endpoint == "organizations/":
            return [{"name": "OrgA"}, {"name": "OrgB"}]
        if endpoint == "job_templates/" and params.get("organization") == 1:
            return [{"id": 10, "name": "Deploy", "project": 8}]
        if endpoint == "teams/" and params.get("organization") == 1:
            raise RuntimeError("team fetch failed")
        if endpoint.endswith("/"):
            return []
        raise AssertionError((endpoint, params))

    async def get(self, endpoint: str):
        if endpoint == "projects/8/":
            return {"id": 8, "name": "Shared Project", "organization": 2}
        if endpoint == "credentials/9/":
            return {"id": 9, "name": "Shared Credential", "organization": 2}
        if endpoint == "organizations/2/":
            return {"id": 2, "name": "SharedOrg"}
        raise RuntimeError(endpoint)


@pytest.mark.asyncio
async def test_analyze_organization_fetches_resources_and_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    analyzer = CrossOrgDependencyAnalyzer(client)

    dep = ResourceDependency("projects", 8, "Shared Project", "SharedOrg")
    dep.add_usage("job_templates", 10, "Deploy")

    async def cached_analyze(org_name, resources):
        return {"SharedOrg": [dep]}

    monkeypatch.setattr(analyzer, "_analyze_resources", cached_analyze)

    report = await analyzer.analyze_organization("OrgA")
    assert report.has_cross_org_deps is True
    assert report.required_migrations_before == ["SharedOrg"]
    assert report.resources["job_templates"] == [{"id": 10, "name": "Deploy", "project": 8}]


@pytest.mark.asyncio
async def test_dependency_analyzer_resource_fetch_helpers_and_analysis() -> None:
    client = FakeClient()
    analyzer = CrossOrgDependencyAnalyzer(client)

    resources = await analyzer._fetch_org_resources(1, "OrgA")
    assert resources["job_templates"] == [{"id": 10, "name": "Deploy", "project": 8}]
    assert resources["teams"] == []

    resource_items = {
        "job_templates": [
            {
                "id": 10,
                "name": "Deploy",
                "project": 8,
                "summary_fields": {"credentials": [{"id": 9}]},
            },
            {"id": 11, "name": "Deploy Again", "project": 8},
        ]
    }
    deps = await analyzer._analyze_resources("OrgA", resource_items)
    assert sorted(deps) == ["SharedOrg"]
    assert len(deps["SharedOrg"]) == 2
    assert deps["SharedOrg"][0].resource_name == "Shared Project"
    assert len(deps["SharedOrg"][0].required_by) == 2

    assert await analyzer._get_resource_org("projects", 8) == "SharedOrg"
    assert await analyzer._get_resource_org(None, 8) is None
    assert await analyzer._get_resource_name(None, 5) == "resource_5"
    assert await analyzer._get_resource_name("missing", 7) == "missing_7"
    assert await analyzer._get_org_name(2) == "SharedOrg"
    assert await analyzer._get_org_name(999) == "org_999"


@pytest.mark.asyncio
async def test_analyze_all_organizations_and_serialize_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    analyzer = CrossOrgDependencyAnalyzer(client)

    org_a = OrgDependencyReport(
        org_name="OrgA",
        org_id=1,
        resource_count=2,
        has_cross_org_deps=False,
        dependencies={},
        can_migrate_standalone=True,
        required_migrations_before=[],
        resources={"projects": [{"id": 1}]},
    )
    org_b = OrgDependencyReport(
        org_name="OrgB",
        org_id=2,
        resource_count=1,
        has_cross_org_deps=True,
        dependencies={"OrgA": [ResourceDependency("projects", 1, "Shared", "OrgA")]},
        can_migrate_standalone=False,
        required_migrations_before=["OrgA"],
        resources={"job_templates": [{"id": 10}]},
    )

    async def fake_analyze(org_name: str):
        if org_name == "OrgA":
            return org_a
        return org_b

    monkeypatch.setattr(analyzer, "analyze_organization", fake_analyze)

    report = await analyzer.analyze_all_organizations()
    assert report.total_organizations == 2
    assert report.independent_orgs == ["OrgA"]
    assert report.dependent_orgs == ["OrgB"]
    assert report.migration_order == ["OrgA", "OrgB"]

    serialized = _serialize_report(report)
    assert serialized["migration_order"] == ["OrgA", "OrgB"]
    assert serialized["organizations"]["OrgA"]["blocks"] == ["OrgB"]
    assert serialized["organizations"]["OrgA"]["quality"] is None
    assert serialized["average_quality_score"] == 100.0


@pytest.mark.asyncio
async def test_analyze_all_organizations_propagates_task_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    analyzer = CrossOrgDependencyAnalyzer(client)

    async def sometimes_fail(org_name: str):
        if org_name == "OrgB":
            raise RuntimeError("boom")
        return OrgDependencyReport(
            org_name=org_name,
            org_id=1,
            resource_count=0,
            has_cross_org_deps=False,
            dependencies={},
            can_migrate_standalone=True,
            required_migrations_before=[],
        )

    monkeypatch.setattr(analyzer, "analyze_organization", sometimes_fail)

    with pytest.raises(RuntimeError, match="boom"):
        await analyzer.analyze_all_organizations()
