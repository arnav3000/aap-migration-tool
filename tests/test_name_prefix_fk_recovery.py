from __future__ import annotations

import pytest

from aap_migration.config import PerformanceConfig
from aap_migration.migration.importer import ProjectImporter, ResourceImporter
from aap_migration.migration.transformer import ProjectTransformer
from aap_migration.utils.naming import apply_name_prefix


class FakeState:
    def __init__(self, migrated=None):
        self.mapped_ids: dict[tuple[str, int], int] = {}
        self.saved: list[dict] = []
        self.source_mappings: set[tuple[str, int]] = set()
        self.migrated = set(migrated or set())

    def is_migrated(self, resource_type, source_id):
        return (resource_type, source_id) in self.migrated

    def get_mapped_id(self, resource_type, source_id):
        return self.mapped_ids.get((resource_type, source_id))

    def has_source_mapping(self, resource_type, source_id):
        return (resource_type, source_id) in self.source_mappings or (
            resource_type,
            source_id,
        ) in self.mapped_ids

    def create_source_mapping(self, resource_type, source_id, source_name=None):
        self.source_mappings.add((resource_type, source_id))

    def save_id_mapping(self, **kwargs):
        self.saved.append(kwargs)
        self.mapped_ids[(kwargs["resource_type"], kwargs["source_id"])] = kwargs["target_id"]

    def mark_in_progress(self, **kwargs):
        return None


class FakeClient:
    def __init__(self, find_results=None):
        self.find_results = dict(find_results or {})
        self.find_calls: list[tuple] = []
        self.update_calls: list[tuple] = []
        self.get_results: dict = {}

    async def find_resource_by_name(
        self, resource_type, name, organization_id=None, parent_id=None, parent_field=None
    ):
        key = (resource_type, name, organization_id)
        self.find_calls.append(key)
        return self.find_results.get(key)

    async def get(self, endpoint, params=None):
        params = params or {}
        return self.get_results[(endpoint, tuple(sorted(params.items())))]

    async def update_resource(self, resource_type, target_id, data):
        self.update_calls.append((resource_type, target_id, dict(data)))
        return {"id": target_id, **data}


class WidgetImporter(ResourceImporter):
    DEPENDENCIES = {"organization": "organizations", "credential": "credentials"}


def test_apply_name_prefix_records_prefix_even_for_managed_credentials() -> None:
    managed = {"name": "Ansible Galaxy", "managed": True}
    apply_name_prefix("credentials", managed, "dev_")
    assert managed["name"] == "Ansible Galaxy"
    assert managed["_name_prefix"] == "dev_"

    custom = {"name": "scm-cred"}
    apply_name_prefix("credentials", custom, "dev_")
    assert custom["name"] == "dev_scm-cred"
    assert custom["_name_prefix"] == "dev_"


def test_project_transformer_stashes_credential_name_for_prefix_recovery() -> None:
    state = FakeState()
    state.source_mappings.add(("organizations", 1))
    transformer = ProjectTransformer(state=state, defer_project_sync=False)

    transformed = transformer.transform_resource(
        "projects",
        {
            "id": 10,
            "name": "My Project",
            "organization": 1,
            "scm_type": "git",
            "scm_url": "https://example.com/repo.git",
            "summary_fields": {
                "organization": {"id": 1, "name": "Default"},
                "credential": {"id": 9, "name": "scm-cred"},
            },
        },
    )

    assert transformed["credential"] == 9
    assert transformed["_dependency_names"]["credential"] == "scm-cred"
    assert "summary_fields" not in transformed


@pytest.mark.asyncio
async def test_resolve_dependencies_recovers_prefixed_credential_by_name() -> None:
    state = FakeState()
    state.mapped_ids[("organizations", 1)] = 101
    client = FakeClient(
        find_results={
            ("credentials", "dev_scm-cred", 101): {"id": 209, "name": "dev_scm-cred"},
        }
    )
    importer = WidgetImporter(client, state, PerformanceConfig(), name_prefix="dev_")

    resolved = await importer._resolve_dependencies(
        "projects",
        {
            "name": "dev_My Project",
            "organization": 1,
            "credential": 9,
            "_name_prefix": "dev_",
            "_dependency_names": {"credential": "scm-cred", "organization": "Default"},
        },
    )

    assert resolved["organization"] == 101
    assert resolved["credential"] == 209
    assert state.get_mapped_id("credentials", 9) == 209
    assert ("credentials", "dev_scm-cred", 101) in client.find_calls
    assert "_dependency_names" not in resolved
    assert "_name_prefix" not in resolved


@pytest.mark.asyncio
async def test_project_importer_attaches_recovered_prefixed_credential() -> None:
    state = FakeState(migrated={("projects", 10)})
    state.mapped_ids[("organizations", 1)] = 101
    state.mapped_ids[("projects", 10)] = 310

    client = FakeClient(
        find_results={
            ("credentials", "dev_scm-cred", 101): {"id": 209, "name": "dev_scm-cred"},
        }
    )
    client.get_results[("projects/310/", ())] = {
        "id": 310,
        "name": "dev_My Project",
        "credential": None,
    }
    importer = ProjectImporter(client, state, PerformanceConfig(), name_prefix="dev_")

    result = await importer.import_resource(
        "projects",
        10,
        {
            "name": "dev_My Project",
            "organization": 1,
            "credential": 9,
            "_name_prefix": "dev_",
            "_dependency_names": {"credential": "scm-cred"},
            "scm_type": "git",
            "scm_url": "https://example.com/repo.git",
        },
    )

    assert result is not None
    assert result["_already_migrated"] is True
    assert "credential attached" in result["_skip_reason"]
    assert client.update_calls == [("projects", 310, {"credential": 209})]
