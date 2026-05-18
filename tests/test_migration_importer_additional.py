import pytest

from aap_migration.client.exceptions import APIError
from aap_migration.config import PerformanceConfig
from aap_migration.migration.importer import (
    ApplicationImporter,
    HostInventoryMembershipImporter,
    InstanceImporter,
    InventoryGroupImporter,
    InventorySourceImporter,
    RBACImporter,
    ScheduleImporter,
    SystemJobTemplateImporter,
)


class FakeState:
    def __init__(self, mapped_ids=None, migrated=None):
        self.database_url = "sqlite:///fake.db"
        self.mapped_ids = dict(mapped_ids or {})
        self.migrated = set(migrated or set())
        self.in_progress = []
        self.completed = []
        self.failed = []
        self.saved = []
        self.created = []

    def is_migrated(self, resource_type, source_id):
        return (resource_type, source_id) in self.migrated

    def mark_in_progress(self, *args, **kwargs):
        self.in_progress.append(args or kwargs)

    def mark_completed(self, *args, **kwargs):
        self.completed.append(args or kwargs)
        if kwargs:
            self.migrated.add((kwargs["resource_type"], kwargs["source_id"]))
            self.mapped_ids[(kwargs["resource_type"], kwargs["source_id"])] = kwargs["target_id"]
        else:
            self.migrated.add((args[0], args[1]))
            self.mapped_ids[(args[0], args[1])] = args[2]

    def mark_failed(self, *args, **kwargs):
        self.failed.append(args or kwargs)

    def save_id_mapping(self, **kwargs):
        self.saved.append(kwargs)
        self.mapped_ids[(kwargs["resource_type"], kwargs["source_id"])] = kwargs["target_id"]

    def get_mapped_id(self, resource_type, source_id):
        return self.mapped_ids.get((resource_type, source_id))

    def create_source_mapping(self, resource_type, source_id, source_name=None, metadata=None):
        self.created.append((resource_type, source_id, source_name, metadata))

    def has_source_mapping(self, resource_type, source_id):
        return any(item[:2] == (resource_type, source_id) for item in self.created)

    def batch_create_mappings(self, mappings, batch_size=100):
        for mapping in mappings:
            self.mapped_ids[(mapping["resource_type"], mapping["source_id"])] = mapping["target_id"]
        return len(mappings)


class FakeTargetClient:
    def __init__(self):
        self.create_result = {"id": 500, "name": "created"}
        self.create_calls = []
        self.post_calls = []
        self.get_responses = {}
        self.post_result = {"id": 700, "name": "posted"}
        self.post_error = None
        self.list_resources_result = []

    async def create_resource(self, resource_type, data, check_exists=True):
        self.create_calls.append((resource_type, dict(data), check_exists))
        return dict(self.create_result)

    async def get(self, endpoint, params=None):
        key = (endpoint, tuple(sorted((params or {}).items())))
        value = self.get_responses[key]
        if isinstance(value, Exception):
            raise value
        return value

    async def post(self, endpoint, json_data=None, data=None):
        payload = dict(json_data or data or {})
        self.post_calls.append((endpoint, payload))
        if self.post_error:
            raise self.post_error
        return dict(self.post_result)

    async def list_resources(self, resource_type):
        assert resource_type == "instances"
        return list(self.list_resources_result)


@pytest.mark.asyncio
async def test_instance_importer_maps_hosts_and_reports_progress():
    state = FakeState(migrated={("instances", 3)})
    client = FakeTargetClient()
    client.list_resources_result = [
        {"id": 501, "hostname": "controller-a"},
        {"id": 502, "hostname": "exact-host"},
    ]
    importer = InstanceImporter(
        client,
        state,
        PerformanceConfig(),
        resource_mappings={"instances": {"legacy-a": "controller-a"}},
    )

    progress = []
    results = await importer.import_instances(
        [
            {"id": 1, "hostname": "legacy-a"},
            {"id": 2, "hostname": "missing-host"},
            {"id": 3, "hostname": "already-migrated"},
            {"hostname": "no-id"},
            {"id": 4, "hostname": "exact-host"},
        ],
        progress_callback=lambda success, failed, skipped: progress.append(
            (success, failed, skipped)
        ),
    )

    assert [item["id"] for item in results] == [501, 502]
    assert importer.stats["imported_count"] == 2
    assert importer.stats["error_count"] == 2
    assert progress[-1] == (2, 2, 1)
    assert state.mapped_ids[("instances", 1)] == 501
    assert any(
        item.get("resource_type") == "instances"
        and item.get("source_id") == 2
        and "Add mapping to config/mappings.yaml" in item.get("error_message", "")
        for item in state.failed
        if isinstance(item, dict)
    )


@pytest.mark.asyncio
async def test_inventory_group_inventory_source_and_schedule_importers(monkeypatch):
    state = FakeState(mapped_ids={("inventories", 10): 110, ("job_templates", 7): 707})
    client = FakeTargetClient()
    importer = InventoryGroupImporter(client, state, PerformanceConfig())

    groups = [
        {"id": 1, "name": "root", "inventory": 10, "children": [2]},
        {"id": 2, "name": "child", "inventory": 10},
    ]
    tiers = importer._topological_sort_tiers(groups)
    assert [[item["id"] for item in tier] for tier in tiers] == [[1], [2]]
    assert "children" not in groups[0]
    assert groups[1]["parent"] == 1

    created = await importer.import_resource(
        "inventory_groups",
        1,
        {"name": "root", "inventory": 10},
    )
    assert created["id"] == 500
    assert client.create_calls[-1][0] == "groups"
    assert client.create_calls[-1][1]["inventory"] == 110

    client.create_result = {"name": "missing-id"}
    with pytest.raises(TypeError, match="no id"):
        await importer.import_resource(
            "inventory_groups",
            2,
            {"name": "broken", "inventory": 10},
        )
    assert any(item["source_id"] == 2 for item in importer.import_errors)

    schedule_importer = ScheduleImporter(client, state, PerformanceConfig())
    resolved = await schedule_importer._resolve_dependencies(
        "schedules",
        {
            "id": 91,
            "_source_id": 91,
            "name": "Nightly",
            "enabled": True,
            "unified_job_template": 7,
            "_ujt_resource_type": "job_templates",
        },
    )
    assert resolved["unified_job_template"] == 707
    assert resolved["enabled"] is False
    assert "_ujt_resource_type" not in resolved

    unresolved = await schedule_importer._resolve_dependencies(
        "schedules",
        {
            "id": 92,
            "enabled": False,
            "unified_job_template": 99,
            "_ujt_resource_type": "job_templates",
        },
    )
    assert unresolved["unified_job_template"] == 99
    assert unresolved["enabled"] is False

    source_client = FakeTargetClient()
    source_client.post_result = {"id": 801, "name": "schedule-created"}
    source_importer = InventorySourceImporter(source_client, state, PerformanceConfig())

    async def fake_import_parallel(
        resource_type, resources, progress_callback=None, concurrency=None
    ):
        assert resource_type == "inventory_sources"
        state.mapped_ids[("inventory_sources", 20)] = 220
        return [{"id": 220, "name": "SCM Source"}]

    monkeypatch.setattr(source_importer, "_import_parallel", fake_import_parallel)
    results = await source_importer.import_inventory_sources(
        [
            {
                "_source_id": 20,
                "name": "SCM Source",
                "schedules": [
                    {"id": 44, "name": "Sync Now", "enabled": True, "unified_job_template": 20}
                ],
            }
        ]
    )

    assert results == [{"id": 220, "name": "SCM Source"}]
    assert source_client.post_calls[0] == (
        "inventory_sources/220/schedules/",
        {"name": "Sync Now", "enabled": False},
    )
    assert source_client.post_calls[1] == ("inventory_sources/220/update/", {})
    assert state.mapped_ids[("schedules", 44)] == 801


@pytest.mark.asyncio
async def test_membership_rbac_system_templates_and_applications():
    performance = PerformanceConfig()

    membership_state = FakeState(mapped_ids={("hosts", 1): 101, ("inventories", 2): 202})
    membership_client = FakeTargetClient()
    membership_client.get_responses[("hosts/101/", ())] = {"inventory": 999}
    membership_client.get_responses[("inventories/202/hosts/", (("id", 101), ("page_size", 1)))] = {
        "count": 0
    }
    membership_importer = HostInventoryMembershipImporter(
        membership_client, membership_state, performance
    )

    created = await membership_importer.import_resource(
        {"host_id": 1, "inventory_id": 2, "host_name": "host-a", "inventory_name": "inv-a"}
    )
    assert created == {"status": "created", "target_id": "101_202"}
    assert membership_client.post_calls[-1] == ("inventories/202/hosts/", {"id": 101})

    membership_state.migrated.clear()
    membership_client.get_responses[("hosts/101/", ())] = {"inventory": 202}
    primary = await membership_importer.import_resource(
        {"host_id": 1, "inventory_id": 2, "host_name": "host-a", "inventory_name": "inv-a"}
    )
    assert primary["reason"] == "already_primary_inventory"

    membership_state.migrated.clear()
    membership_client.get_responses[("hosts/101/", ())] = {"inventory": 999}
    membership_client.get_responses[("inventories/202/hosts/", (("id", 101), ("page_size", 1)))] = {
        "count": 1
    }
    existing = await membership_importer.import_resource(
        {"host_id": 1, "inventory_id": 2, "host_name": "host-a", "inventory_name": "inv-a"}
    )
    assert existing["reason"] == "already_in_inventory"

    failing_state = FakeState(mapped_ids={("hosts", 1): 101, ("inventories", 2): 202})
    failing_client = FakeTargetClient()
    failing_client.get_responses[("hosts/101/", ())] = {"inventory": 999}
    failing_client.get_responses[("inventories/202/hosts/", (("id", 101), ("page_size", 1)))] = {
        "count": 0
    }
    failing_client.post_error = APIError("membership boom")
    failing_importer = HostInventoryMembershipImporter(failing_client, failing_state, performance)
    failed = await failing_importer.import_resource(
        {"host_id": 1, "inventory_id": 2, "host_name": "host-a", "inventory_name": "inv-a"}
    )
    assert failed["status"] == "failed"
    assert failing_importer.import_errors[-1]["error_type"] == "APIError"

    rbac_state = FakeState(
        mapped_ids={
            ("organizations", 1): 1001,
            ("users", 2): 2002,
            ("teams", 3): 3003,
        }
    )
    rbac_client = FakeTargetClient()
    rbac_importer = RBACImporter(rbac_client, rbac_state, performance)
    rbac_client.post_result = {"status": "ok"}
    results = await rbac_importer.import_role_assignments(
        [
            {"resource_type": "organizations", "resource_id": 1, "role": "admin", "user": 2},
            {"resource_type": "organizations", "resource_id": 1, "role": "member", "team": 3},
            {"resource_type": "organizations", "resource_id": 999, "role": "admin", "user": 2},
            {"resource_type": "organizations", "resource_id": 1, "role": "member"},
        ]
    )
    assert len(results) == 2
    assert rbac_client.post_calls[:2] == [
        ("organizations/1001/roles/admin/users/", {"id": 2002}),
        ("organizations/1001/roles/member/teams/", {"id": 3003}),
    ]

    template_state = FakeState()
    template_client = FakeTargetClient()
    template_client.get_responses[("system_job_templates/", (("name", "Cleanup"),))] = {
        "results": [{"id": 11}]
    }
    template_client.get_responses[("system_job_templates/", (("name", "Missing"),))] = {
        "results": []
    }
    template_importer = SystemJobTemplateImporter(template_client, template_state, performance)
    mapped = await template_importer.import_resource("system_job_templates", 5, {"name": "Cleanup"})
    missing = await template_importer.import_resource(
        "system_job_templates", 6, {"name": "Missing"}
    )
    assert mapped == {"id": 11, "name": "Cleanup"}
    assert missing is None
    assert template_state.mapped_ids[("system_job_templates", 5)] == 11

    app_state = FakeState(mapped_ids={("organizations", 7): 107})
    app_client = FakeTargetClient()
    app_client.post_result = {
        "id": 77,
        "name": "Portal App",
        "client_id": "new-client",
        "client_secret": "new-secret",
    }
    app_importer = ApplicationImporter(app_client, app_state, performance)
    app = await app_importer.import_resource(
        "applications",
        70,
        {
            "name": "Portal App",
            "organization": 7,
            "client_id": "old-client",
            "client_secret": "$encrypted$",
            "_requires_new_secret": True,
            "_source_id": 70,
        },
    )
    assert app["id"] == 77
    assert app_client.post_calls[-1] == (
        "applications/",
        {"name": "Portal App", "organization": 107},
    )
    assert app_importer.import_errors[-1]["action_required"] == "UPDATE_EXTERNAL_SYSTEMS"
