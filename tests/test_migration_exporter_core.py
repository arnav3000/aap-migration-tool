import pytest

from aap_migration.client.exceptions import APIError
from aap_migration.config import PerformanceConfig
from aap_migration.migration.exporter import (
    ApplicationExporter,
    CredentialExporter,
    CredentialInputSourceExporter,
    HostExporter,
    HostInventoryMembershipExporter,
    InventoryExporter,
    JobTemplateExporter,
    ResourceExporter,
    SettingsExporter,
    WorkflowExporter,
    create_exporter,
)


class FakeState:
    def __init__(self, max_exported=None):
        self.max_exported = dict(max_exported or {})
        self.export_failures = []
        self.mapping_batches = []

    def mark_export_failed(self, **kwargs):
        self.export_failures.append(kwargs)

    def get_max_exported_id(self, resource_type):
        return self.max_exported.get(resource_type)

    def batch_create_mappings(self, mappings, batch_size=100):
        self.mapping_batches.append((list(mappings), batch_size))


class FakeSourceClient:
    def __init__(self):
        self.responses = {}
        self.parallel_items = {}
        self.get_calls = []
        self.parallel_calls = []
        self.job_template_credentials = {}
        self.workflow_nodes = {}
        self.base_url = "https://source.example.com"

    def add_response(self, endpoint, response, params=None):
        key = (endpoint, tuple(sorted((params or {}).items())))
        self.responses.setdefault(key, []).append(response)

    def add_parallel_items(self, endpoint, items, params=None):
        key = (endpoint, tuple(sorted((params or {}).items())))
        self.parallel_items[key] = list(items)

    async def get(self, endpoint, params=None):
        params = params or {}
        key = (endpoint, tuple(sorted(params.items())))
        self.get_calls.append((endpoint, dict(params)))
        queue = self.responses.get(key)
        if queue is None:
            raise AssertionError(f"Unexpected GET {endpoint} with params {params}")
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def get_all_resources_parallel(self, endpoint, page_size=200, max_concurrent=5, **params):
        key = (endpoint, tuple(sorted(params.items())))
        self.parallel_calls.append((endpoint, page_size, max_concurrent, dict(params)))
        items = self.parallel_items.get(key)
        if items is None:
            raise AssertionError(f"Unexpected parallel GET {endpoint} with params {params}")
        for item in items:
            if isinstance(item, Exception):
                raise item
            yield item

    async def get_job_template_credentials(self, template_id):
        value = self.job_template_credentials[template_id]
        if isinstance(value, Exception):
            raise value
        return value

    async def get_workflow_nodes(self, workflow_id):
        value = self.workflow_nodes[workflow_id]
        if isinstance(value, Exception):
            raise value
        return value


async def collect(async_iterable):
    results = []
    async for item in async_iterable:
        results.append(item)
    return results


@pytest.mark.asyncio
async def test_resource_exporter_retries_sequential_export_and_early_stop(monkeypatch):
    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr("aap_migration.migration.exporter.asyncio.sleep", fake_sleep)

    client = FakeSourceClient()
    state = FakeState()
    exporter = ResourceExporter(client, state, PerformanceConfig())

    client.add_response(
        "inventories/",
        APIError("temporary", status_code=502),
        params={"page": 1, "page_size": 1},
    )
    client.add_response(
        "inventories/",
        {"count": 5},
        params={"page": 1, "page_size": 1},
    )
    assert await exporter.get_count("inventories/") == 5

    client.add_response(
        "inventories/",
        {
            "count": 3,
            "next": "page-2",
            "results": [{"id": 1, "name": "one"}, {"name": "missing-id"}],
        },
        params={"page": 1, "page_size": 100},
    )
    client.add_response(
        "inventories/",
        {
            "count": 3,
            "next": None,
            "results": [{"id": 2, "name": "two"}],
        },
        params={"page": 2, "page_size": 100},
    )

    resources = await collect(
        exporter.export_resources("inventories", "inventories/", page_size=100)
    )
    assert [item["id"] for item in resources] == [1, 2]
    assert exporter.stats["exported_count"] == 2
    assert exporter.stats["skipped_count"] == 1

    failing_client = FakeSourceClient()
    failing_exporter = ResourceExporter(failing_client, state, PerformanceConfig())
    for _ in range(5):
        failing_client.add_response(
            "projects/",
            APIError("gateway", status_code=503),
            params={"page": 1, "page_size": 100},
        )

    assert (
        await collect(failing_exporter.export_resources("projects", "projects/", page_size=100))
        == []
    )
    assert failing_exporter.stats["error_count"] == 1


@pytest.mark.asyncio
async def test_inventory_and_host_exporters_apply_filters_and_attach_related_data():
    inventory_client = FakeSourceClient()
    state = FakeState()

    inventory_client.add_response(
        "inventory_sources/",
        {
            "results": [
                {"id": 91, "inventory": 1},
                {"id": 92, "inventory": 1},
                {"id": 93, "inventory": 2},
            ],
            "next": None,
        },
        params={"page": 1, "page_size": 200},
    )
    inventory_client.add_response(
        "inventories/",
        {"count": 1, "results": [{"id": 1, "name": "Primary"}], "next": None},
        params={
            "page": 1,
            "page_size": 200,
            "inventory_sources__isnull": "true",
            "pending_deletion": "false",
            "kind": "",
        },
    )

    inventory_exporter = InventoryExporter(inventory_client, state, PerformanceConfig())
    inventory_exporter.set_skip_smart_inventories(True)
    inventories = await collect(inventory_exporter.export(include_sources=True))
    assert inventories[0]["sources"] == [{"id": 91, "inventory": 1}, {"id": 92, "inventory": 1}]

    host_client = FakeSourceClient()
    host_client.add_response(
        "inventories/1/hosts/",
        {"count": 1, "results": [{"id": 11, "name": "host-a"}], "next": None},
        params={"page": 1, "page_size": 200, "inventory_sources__isnull": "true"},
    )
    host_client.add_response(
        "inventories/2/hosts/",
        {"count": 1, "results": [{"id": 12, "name": "host-b"}], "next": None},
        params={"page": 1, "page_size": 200, "inventory_sources__isnull": "true"},
    )

    host_exporter = HostExporter(host_client, state, PerformanceConfig())
    host_exporter.set_skip_dynamic_hosts(True)
    hosts = await collect(host_exporter.export_by_inventory([1, 2]))
    assert hosts == [
        {"id": 11, "name": "host-a", "inventory_id": 1},
        {"id": 12, "name": "host-b", "inventory_id": 2},
    ]


@pytest.mark.asyncio
async def test_membership_and_credential_related_exporters_use_caches():
    state = FakeState()

    membership_client = FakeSourceClient()
    membership_client.add_response(
        "inventories/",
        {
            "count": 3,
            "results": [
                {"id": 1, "name": "Regular", "kind": ""},
                {"id": 2, "name": "Smart", "kind": "smart"},
                {"id": 3, "name": "Regular 2", "kind": "regular"},
            ],
            "next": None,
        },
        params={"page": 1, "page_size": 200},
    )
    membership_client.add_response(
        "inventories/1/hosts/",
        {
            "count": 3,
            "results": [
                {"id": 10, "name": "alpha"},
                {"id": 11, "name": "beta"},
                {"id": 11, "name": "beta"},
            ],
            "next": None,
        },
        params={"page": 1, "page_size": 200, "inventory_sources__isnull": "true"},
    )
    membership_client.add_response(
        "inventories/3/hosts/",
        {
            "count": 2,
            "results": [{"id": 11, "name": "beta"}, {"id": 12, "name": "gamma"}],
            "next": None,
        },
        params={"page": 1, "page_size": 200, "inventory_sources__isnull": "true"},
    )

    membership_exporter = HostInventoryMembershipExporter(
        membership_client,
        state,
        PerformanceConfig(),
    )
    assert await membership_exporter.get_count("") == 1
    memberships = await collect(membership_exporter.export())
    assert memberships == [
        {"host_id": 10, "inventory_id": 1, "host_name": "alpha", "inventory_name": "Regular"},
        {"host_id": 11, "inventory_id": 1, "host_name": "beta", "inventory_name": "Regular"},
        {"host_id": 11, "inventory_id": 3, "host_name": "beta", "inventory_name": "Regular 2"},
        {"host_id": 12, "inventory_id": 3, "host_name": "gamma", "inventory_name": "Regular 2"},
    ]

    credential_client = FakeSourceClient()
    credential_client.add_response(
        "credential_types/",
        {"results": [{"id": 5, "name": "Machine"}], "next": None},
        params={"page": 1, "page_size": 200},
    )
    credential_client.add_response(
        "credentials/",
        {
            "count": 2,
            "results": [
                {
                    "id": 20,
                    "name": "Cred",
                    "credential_type": 5,
                    "inputs": {"password": "$encrypted$"},
                },
                {"id": 21, "name": "Unknown", "credential_type": 999, "inputs": {}},
            ],
            "next": None,
        },
        params={"page": 1, "page_size": 200},
    )

    credential_exporter = CredentialExporter(credential_client, state, PerformanceConfig())
    credentials = await collect(credential_exporter.export(include_types=True))
    assert credentials[0]["_encrypted_fields"] == ["password"]
    assert credentials[0]["credential_type_details"]["name"] == "Machine"
    assert "credential_type_details" not in credentials[1]

    input_source_client = FakeSourceClient()
    input_source_client.add_response(
        "credential_types/",
        {"results": [{"id": 5, "name": "Machine"}], "next": None},
        params={"page": 1, "page_size": 200},
    )
    input_source_client.add_response(
        "credentials/",
        {
            "results": [
                {"id": 20, "name": "Target", "credential_type": 5},
                {"id": 21, "name": "Source", "credential_type": 5},
            ],
            "next": None,
        },
        params={"page": 1, "page_size": 200},
    )
    input_source_client.add_response(
        "credential_input_sources/",
        {
            "count": 1,
            "results": [{"id": 99, "target_credential": 20, "source_credential": 21}],
            "next": None,
        },
        params={"page": 1, "page_size": 100},
    )

    input_source_exporter = CredentialInputSourceExporter(
        input_source_client,
        state,
        PerformanceConfig(),
    )
    input_sources = await collect(input_source_exporter.export(include_details=True))
    assert input_sources[0]["target_credential_details"]["name"] == "Target"
    assert input_sources[0]["source_credential_details"]["name"] == "Source"
    assert input_sources[0]["source_credential_type_details"]["name"] == "Machine"


@pytest.mark.asyncio
async def test_parallel_export_job_templates_workflows_settings_and_factory(monkeypatch):
    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr("aap_migration.migration.exporter.asyncio.sleep", fake_sleep)

    state = FakeState()
    performance = PerformanceConfig()

    parallel_client = FakeSourceClient()
    parallel_client.add_parallel_items(
        "hosts/",
        [{"id": 11, "name": "host-a"}, {"id": 12, "name": "host-b", "_skipped": True}],
        params={"inventory_sources__isnull": "true", "id__gt": 10, "order_by": "id"},
    )
    host_exporter = HostExporter(parallel_client, state, performance)
    host_exporter.set_skip_dynamic_hosts(True)
    host_exporter.set_resume_checkpoint(10)
    parallel_hosts = await collect(host_exporter.export_parallel("hosts", "hosts/"))
    assert parallel_hosts[0]["id"] == 11
    assert host_exporter.get_stats()["exported_count"] == 1
    assert host_exporter.get_stats()["skipped_count"] == 1

    job_client = FakeSourceClient()
    job_client.add_response(
        "job_templates/",
        {
            "count": 2,
            "results": [
                {"id": 1, "name": "One", "summary_fields": {"credentials": [{"id": 7}]}},
                {"id": 2, "name": "Two"},
            ],
            "next": None,
        },
        params={"page": 1, "page_size": 200},
    )
    job_client.job_template_credentials[2] = [{"id": 22}, {"id": 23}]
    job_client.add_response("job_templates/1/schedules/", {"results": [{"id": 1001}]})
    job_client.add_response("job_templates/2/schedules/", {"results": []})
    job_client.add_response("job_templates/1/notification_templates_started/", {"results": []})
    job_client.add_response(
        "job_templates/1/notification_templates_success/", {"results": [{"id": 90}]}
    )
    job_client.add_response("job_templates/1/notification_templates_error/", Exception("boom"))
    job_client.add_response("job_templates/2/notification_templates_started/", {"results": []})
    job_client.add_response("job_templates/2/notification_templates_success/", {"results": []})
    job_client.add_response("job_templates/2/notification_templates_error/", {"results": []})
    job_client.add_response("job_templates/1/survey_spec/", {"spec": [{"question_name": "Q1"}]})
    job_client.add_response("job_templates/2/survey_spec/", Exception("404 not found"))

    job_exporter = JobTemplateExporter(job_client, state, performance)
    templates = await collect(job_exporter.export(include_credentials=True))
    assert templates[0]["_credentials"] == [7]
    assert templates[0]["notifications"] == {"notification_templates_success": [90]}
    assert templates[0]["survey_spec"]["spec"][0]["question_name"] == "Q1"
    assert templates[1]["_credentials"] == [22, 23]
    assert "survey_spec" not in templates[1]

    workflow_client = FakeSourceClient()
    workflow_client.add_parallel_items(
        "workflow_job_templates/",
        [{"id": 55, "name": "WF", "survey_enabled": True}],
        params={},
    )
    workflow_client.workflow_nodes[55] = [{"id": 500, "identifier": "root"}]
    workflow_client.add_response(
        "workflow_job_templates/55/survey_spec/", {"spec": [{"question_name": "wf"}]}
    )
    workflow_client.add_response("workflow_job_templates/55/schedules/", {"results": [{"id": 700}]})
    workflow_client.add_response(
        "workflow_job_templates/55/notification_templates_started/",
        {"results": []},
    )
    workflow_client.add_response(
        "workflow_job_templates/55/notification_templates_success/",
        {"results": [{"id": 77}]},
    )
    workflow_client.add_response(
        "workflow_job_templates/55/notification_templates_error/",
        {"results": []},
    )
    workflow_client.add_response(
        "workflow_job_templates/55/notification_templates_approvals/",
        {"results": [{"id": 88}]},
    )

    workflow_exporter = WorkflowExporter(workflow_client, state, performance)
    workflows = await collect(
        workflow_exporter.export_parallel(
            "workflow_job_templates",
            "workflow_job_templates/",
        )
    )
    assert workflows[0]["nodes"] == [{"id": 500, "identifier": "root"}]
    assert workflows[0]["notifications"] == {
        "notification_templates_success": [77],
        "notification_templates_approvals": [88],
    }
    assert workflows[0]["schedules"] == [{"id": 700}]

    settings_client = FakeSourceClient()
    settings_client.add_response(
        "settings/all/",
        {"UI_LANDING_PAGE": "jobs", "GITHUB_TOKEN": "masked"},
    )
    settings_exporter = SettingsExporter(settings_client, state, performance)
    assert await settings_exporter.get_count("settings/all/") == 1
    settings = await collect(settings_exporter.export_parallel("settings", "settings/all/"))
    assert settings[0]["_migration_metadata"]["source_url"] == "https://source.example.com"
    assert settings[0]["UI_LANDING_PAGE"] == "jobs"

    app_client = FakeSourceClient()
    app_client.add_response(
        "applications/",
        {
            "count": 1,
            "results": [{"id": 9, "name": "App", "client_secret": "present"}],
            "next": None,
        },
        params={"page": 1, "page_size": 50},
    )
    app_exporter = ApplicationExporter(app_client, state, performance)
    apps = await collect(app_exporter.export())
    assert apps[0]["_has_client_secret"] is True

    assert isinstance(
        create_exporter("settings", settings_client, state, performance), SettingsExporter
    )
    with pytest.raises(NotImplementedError):
        create_exporter("not-a-real-type", settings_client, state, performance)
