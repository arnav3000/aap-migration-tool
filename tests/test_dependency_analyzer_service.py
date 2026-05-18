from __future__ import annotations

from types import SimpleNamespace

import pytest

import aap_migration.api.models as api_models
from aap_migration.analysis.dependency_analyzer import (
    CrossOrgDependencyAnalyzer,
    OrgDependencyReport,
    ResourceDependency,
)
from aap_migration.analysis.quality import DuplicateResource, NamingPattern, QualityReport

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
        if endpoint == "credential_types/" and params.get("organization__isnull") == "true":
            raise RuntimeError("unsupported filter")
        if endpoint == "credential_types/":
            return [{"id": 1, "organization": None}, {"id": 2, "organization": 7}]
        if endpoint == "instance_groups/" and params.get("organization__isnull") == "true":
            raise RuntimeError("broken")
        if endpoint == "instance_groups/":
            raise RuntimeError("broken")
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


class FakeDbService:
    def __init__(self):
        self.bulk_calls = []
        self.upsert_calls = []

    def needs_analysis(self, org_name: str, ttl: int) -> bool:
        return False

    def get_cached_analysis(self, org_name: str):
        return {
            "org_id": 1,
            "resource_count": 1,
            "resources": {"job_templates": [{"id": 10, "name": "Deploy", "project": 8}]},
        }

    def upsert_organization(self, **kwargs):
        self.upsert_calls.append(kwargs)
        return SimpleNamespace(id=99)

    def bulk_upsert_resources(self, **kwargs):
        self.bulk_calls.append(kwargs)


@pytest.mark.asyncio
async def test_analyze_organization_uses_cache_and_fresh_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    db_service = FakeDbService()
    analyzer = CrossOrgDependencyAnalyzer(client, db_service=db_service, use_cache=True)

    dep = ResourceDependency("projects", 8, "Shared Project", "SharedOrg")
    dep.add_usage("job_templates", 10, "Deploy")

    async def cached_analyze(org_name, resources):
        return {"SharedOrg": [dep]}

    monkeypatch.setattr(analyzer, "_analyze_resources", cached_analyze)

    cached = await analyzer.analyze_organization("OrgA")
    assert cached.has_cross_org_deps is True
    assert cached.required_migrations_before == ["SharedOrg"]

    analyzer.use_cache = True
    monkeypatch.setattr(db_service, "needs_analysis", lambda org_name, ttl: True)
    monkeypatch.setattr(
        "aap_migration.analysis.dependency_analyzer.generate_quality_report",
        lambda resources, org_name: QualityReport(
            org_name=org_name, duplicate_count=0, quality_score=99.0
        ),
    )

    async def fresh_analyze(org_name, resources):
        return {}

    monkeypatch.setattr(analyzer, "_analyze_resources", fresh_analyze)

    fresh = await analyzer.analyze_organization("OrgA", force_refresh=True)
    assert fresh.can_migrate_standalone is True
    assert fresh.quality_report is not None
    assert db_service.upsert_calls and db_service.bulk_calls


@pytest.mark.asyncio
async def test_dependency_analyzer_resource_fetch_helpers_and_analysis() -> None:
    client = FakeClient()
    analyzer = CrossOrgDependencyAnalyzer(client)

    resources = await analyzer._fetch_org_resources(1, "OrgA")
    assert resources["job_templates"] == [{"id": 10, "name": "Deploy", "project": 8}]
    assert resources["teams"] == []

    global_resources = await analyzer._fetch_global_resources()
    assert global_resources["credential_types"] == [{"id": 1, "organization": None}]
    assert global_resources["instance_groups"] == []

    resource_items = {
        "job_templates": [
            {"id": 10, "name": "Deploy", "project": 8, "credential": 9},
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
    progress_updates = []
    analyzer = CrossOrgDependencyAnalyzer(
        client,
        progress_callback=lambda current, total, message: progress_updates.append(
            (current, total, message)
        ),
    )

    org_a = OrgDependencyReport(
        org_name="OrgA",
        org_id=1,
        resource_count=2,
        has_cross_org_deps=False,
        dependencies={},
        can_migrate_standalone=True,
        required_migrations_before=[],
        resources={"projects": [{"id": 1}]},
        quality_report=QualityReport(
            org_name="OrgA",
            duplicate_count=1,
            quality_score=91.0,
            duplicates=[
                DuplicateResource(
                    name="dupe",
                    resource_type="projects",
                    count=2,
                    ids=[1, 2],
                    severity="warning",
                    impact="confusing",
                    recommendation="rename",
                )
            ],
            naming_pattern=NamingPattern(total_resources=2, dominant_pattern="snake_case"),
        ),
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
        quality_report=QualityReport(org_name="OrgB", duplicate_count=0, quality_score=100.0),
    )

    async def fake_analyze(org_name: str, current: int, total: int):
        if org_name == "OrgA":
            return org_name, org_a
        return org_name, org_b

    monkeypatch.setattr(analyzer, "_analyze_org_with_progress", fake_analyze)

    async def fake_global_resources():
        return {"credential_types": [{"id": 1}], "execution_environments": []}

    monkeypatch.setattr(analyzer, "_fetch_global_resources", fake_global_resources)

    report = await analyzer.analyze_all_organizations()
    assert report.total_organizations == 2
    assert report.independent_orgs == ["OrgA"]
    assert report.dependent_orgs == ["OrgB"]
    assert report.migration_order == ["OrgA", "OrgB"]
    assert report.total_duplicates == 1
    assert report.average_quality_score == 95.5
    assert progress_updates[0] == (0, 2, "Starting analysis...")

    serialized = _serialize_report(report)
    assert serialized["migration_order"] == ["OrgA", "OrgB"]
    assert serialized["organizations"]["OrgA"]["blocks"] == ["OrgB"]
    assert serialized["organizations"]["OrgA"]["quality"]["duplicate_count"] == 1
    assert serialized["global_resources"]["credential_types"] == 1
    assert serialized["quality_summary"]["average_quality_score"] == 95.5


@pytest.mark.asyncio
async def test_analyze_all_organizations_handles_task_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    analyzer = CrossOrgDependencyAnalyzer(client)

    async def sometimes_fail(org_name: str, current: int, total: int):
        if org_name == "OrgB":
            raise RuntimeError("boom")
        return (
            org_name,
            OrgDependencyReport(
                org_name=org_name,
                org_id=1,
                resource_count=0,
                has_cross_org_deps=False,
                dependencies={},
                can_migrate_standalone=True,
                required_migrations_before=[],
            ),
        )

    monkeypatch.setattr(analyzer, "_analyze_org_with_progress", sometimes_fail)

    async def empty_global_resources():
        return {}

    monkeypatch.setattr(analyzer, "_fetch_global_resources", empty_global_resources)

    report = await analyzer.analyze_all_organizations()
    assert report.analyzed_organizations == ["OrgA", "OrgB"]
    assert report.independent_orgs == ["OrgA"]
    assert report.dependent_orgs == []
