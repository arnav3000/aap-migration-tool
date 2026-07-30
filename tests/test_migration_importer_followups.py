import contextlib
from types import SimpleNamespace

import pytest

from aap_migration.config import PerformanceConfig
from aap_migration.migration.importer import (
    CredentialInputSourceImporter,
    JobTemplateImporter,
    SettingsImporter,
    WorkflowImporter,
)


class FakeState:
    def __init__(self, mapped_ids=None):
        self.database_url = "sqlite:///fake.db"
        self.mapped_ids = dict(mapped_ids or {})
        self.completed = []
        self.failed = []
        self.saved = []
        self.in_progress = []

    def is_migrated(self, resource_type, source_id):
        return False

    def mark_in_progress(self, *args, **kwargs):
        self.in_progress.append(args or kwargs)

    def mark_completed(self, **kwargs):
        self.completed.append(kwargs)
        self.mapped_ids[(kwargs["resource_type"], kwargs["source_id"])] = kwargs["target_id"]

    def mark_failed(self, *args, **kwargs):
        self.failed.append(args or kwargs)

    def save_id_mapping(self, **kwargs):
        self.saved.append(kwargs)
        self.mapped_ids[(kwargs["resource_type"], kwargs["source_id"])] = kwargs["target_id"]

    def get_mapped_id(self, resource_type, source_id):
        return self.mapped_ids.get((resource_type, source_id))


class FakeClient:
    def __init__(self):
        self.post_calls = []
        self.patch_calls = []
        self.get_calls = []

    async def post(self, endpoint, json_data=None):
        self.post_calls.append((endpoint, dict(json_data or {})))
        if endpoint.endswith("/error/"):
            raise RuntimeError("boom")
        return {"id": 900 + len(self.post_calls), "name": "created"}

    async def get(self, endpoint, params=None):
        self.get_calls.append((endpoint, dict(params or {})))
        return {"inputs": {"existing": "value"}, "name": "Vault Target"}

    async def patch(self, endpoint, json_data=None):
        self.patch_calls.append((endpoint, dict(json_data or {})))
        if endpoint.endswith("/999/"):
            raise RuntimeError("patch failed")
        return {"ok": True}

    async def get_version(self):
        return "2.6.1"


class FakeScalarQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self.rows


class FakeFailedDepSession:
    def __init__(self, failed_jobs, failed_workflows):
        self.failed_jobs = failed_jobs
        self.failed_workflows = failed_workflows
        self.call_count = 0

    def query(self, field):
        self.call_count += 1
        rows = self.failed_jobs if self.call_count == 1 else self.failed_workflows
        return FakeScalarQuery([SimpleNamespace(source_id=value) for value in rows])


@pytest.mark.asyncio
async def test_job_template_importer_followup_resources(monkeypatch):
    state = FakeState(mapped_ids={("notification_templates", 5): 105})
    client = FakeClient()
    importer = JobTemplateImporter(client, state, PerformanceConfig())

    associated = []
    warnings = {}

    async def fake_import_resource(resource_type, source_id, data, resolve_dependencies=True):
        if source_id == 2:
            raise RuntimeError("job template failed")
        return {"id": 301, "name": data["name"]}

    async def fake_associate_credentials(template_id, credentials, template_name=None):
        associated.append((template_id, credentials, template_name))

    monkeypatch.setattr(importer, "import_resource", fake_import_resource)
    monkeypatch.setattr(importer, "_associate_credentials", fake_associate_credentials)
    monkeypatch.setattr(
        importer, "_add_notification_warnings", lambda resource_type, data: warnings.update(data)
    )

    progress = []
    results = await importer.import_job_templates(
        [
            {
                "_source_id": 1,
                "name": "JT One",
                "credentials": [{"id": 22}],
                "schedules": [
                    {"id": 11, "name": "Nightly", "enabled": True, "unified_job_template": 1}
                ],
                "survey_spec": {"spec": [{"question_name": "q1"}]},
                "notifications": {"notification_templates_started": [5, 999]},
            },
            {"_source_id": 2, "name": "Broken JT"},
        ],
        progress_callback=lambda success, failed, skipped: progress.append(
            (success, failed, skipped)
        ),
    )

    assert results == [{"id": 301, "name": "JT One"}]
    assert associated == [(301, [{"id": 22}], "JT One")]
    assert client.post_calls[0] == (
        "job_templates/301/schedules/",
        {"name": "Nightly", "enabled": False},
    )
    assert client.post_calls[1] == (
        "job_templates/301/survey_spec/",
        {"spec": [{"question_name": "q1"}]},
    )
    assert client.post_calls[2] == (
        "job_templates/301/notification_templates_started/",
        {"id": 105},
    )
    assert warnings == {
        1: [
            "Notification template (source ID: 999) not migrated - notification_templates_started notification not associated"
        ]
    }
    assert state.mapped_ids[("schedules", 11)] == 901
    assert progress[-1] == (1, 1, 0)


@pytest.mark.asyncio
async def test_workflow_importer_followup_nodes_and_dependencies(monkeypatch):
    state = FakeState(mapped_ids={("notification_templates", 5): 105})
    client = FakeClient()
    importer = WorkflowImporter(client, state, PerformanceConfig())

    fake_session = FakeFailedDepSession(failed_jobs={77}, failed_workflows=set())
    monkeypatch.setattr(
        "aap_migration.migration.importer.get_session",
        lambda _db_url: contextlib.nullcontext(fake_session),
    )
    monkeypatch.setattr(
        "aap_migration.migration.importer.MigrationProgress",
        SimpleNamespace(source_id="source_id", resource_type="resource_type", status="status"),
    )

    async def fake_import_resource(resource_type, source_id, data):
        return {"id": 401, "name": data["name"], "_source_id": source_id}

    monkeypatch.setattr(importer, "import_resource", fake_import_resource)

    class FakeNodeImporter:
        def __init__(self, client, state, performance_config):
            self.import_errors = []

        async def import_workflow_nodes(self, nodes, progress_callback=None):
            return [
                {
                    "id": 501,
                    "_source_id": 91,
                    "_edge_data": {"success_nodes": [], "failure_nodes": [], "always_nodes": []},
                }
            ]

    edge_calls = []
    warnings = {}
    monkeypatch.setattr("aap_migration.migration.importer.WorkflowNodeImporter", FakeNodeImporter)

    async def fake_create_workflow_edges(nodes):
        edge_calls.append(nodes)

    monkeypatch.setattr(importer, "_create_workflow_edges", fake_create_workflow_edges)
    monkeypatch.setattr(
        importer, "_add_notification_warnings", lambda resource_type, data: warnings.update(data)
    )

    progress = []
    results = await importer.import_workflows(
        [
            {
                "_source_id": 10,
                "name": "Blocked WF",
                "_workflow_nodes": [
                    {
                        "unified_job_template": 77,
                        "summary_fields": {
                            "unified_job_template": {"unified_job_type": "job", "name": "Bad JT"}
                        },
                    }
                ],
            },
            {
                "_source_id": 11,
                "name": "WF One",
                "_workflow_nodes": [
                    {
                        "_source_id": 91,
                        "identifier": "node-1",
                        "success_nodes": [],
                        "failure_nodes": [],
                        "always_nodes": [],
                    }
                ],
                "survey_spec": {"spec": [{"question_name": "approve"}]},
                "schedules": [
                    {"id": 12, "name": "WF Schedule", "enabled": True, "unified_job_template": 11}
                ],
                "notifications": {"notification_templates_success": [5, 999]},
            },
        ],
        progress_callback=lambda success, failed, skipped: progress.append(
            (success, failed, skipped)
        ),
    )

    assert results == [{"id": 401, "name": "WF One", "_source_id": 11}]
    assert any(
        item["resource_type"] == "workflow_job_templates" and item["source_id"] == 10
        for item in state.failed
        if isinstance(item, dict)
    )
    assert client.post_calls[0] == (
        "workflow_job_templates/401/survey_spec/",
        {"spec": [{"question_name": "approve"}]},
    )
    assert client.post_calls[1] == (
        "workflow_job_templates/401/schedules/",
        {"name": "WF Schedule", "enabled": False},
    )
    assert client.post_calls[2] == (
        "workflow_job_templates/401/notification_templates_success/",
        {"id": 105},
    )
    assert warnings == {
        11: [
            "Notification template (source ID: 999) not migrated - notification_templates_success notification not associated"
        ]
    }
    assert state.mapped_ids[("schedules", 12)] == 902
    assert progress[-1] == (1, 1, 0)


@pytest.mark.asyncio
async def test_credential_input_sources_and_settings_importers(monkeypatch):
    state = FakeState(mapped_ids={("credentials", 10): 110, ("credentials", 20): 220})
    client = FakeClient()
    cis_importer = CredentialInputSourceImporter(client, state, PerformanceConfig())

    progress = []
    results = await cis_importer.import_credential_input_sources(
        [
            {"_source_id": 1},
            {
                "_source_id": 2,
                "credential": 999,
                "input_field_name": "token",
                "source_credential": 20,
                "source_credential_field_name": "vault",
            },
            {
                "_source_id": 3,
                "credential": 10,
                "input_field_name": "token",
                "source_credential": 999,
                "source_credential_field_name": "vault",
            },
            {
                "_source_id": 4,
                "credential": 10,
                "input_field_name": "token",
                "source_credential": 20,
                "source_credential_field_name": "vault",
            },
        ],
        progress_callback=lambda success, failed, skipped: progress.append(
            (success, failed, skipped)
        ),
    )
    assert results == [{"id": 110, "name": "Vault Target"}]
    assert client.patch_calls[-1] == (
        "credentials/110/",
        {"inputs": {"existing": "value", "token": "$220.vault$"}},
    )
    assert progress[-1] == (1, 3, 0)

    failing_client = FakeClient()
    failing_state = FakeState(mapped_ids={("credentials", 99): 999, ("credentials", 20): 220})
    failing_importer = CredentialInputSourceImporter(
        failing_client, failing_state, PerformanceConfig()
    )
    failed = await failing_importer.import_credential_input_sources(
        [
            {
                "_source_id": 5,
                "credential": 99,
                "input_field_name": "token",
                "source_credential": 20,
                "source_credential_field_name": "vault",
            }
        ]
    )
    assert failed == []
    assert any(item["source_id"] == 5 for item in failing_state.failed if isinstance(item, dict))

    settings_state = FakeState()
    settings_client = FakeClient()
    settings_importer = SettingsImporter(settings_client, settings_state, PerformanceConfig())
    reports = []

    async def fake_migrate_auth(safe, review_required, sensitive):
        return {
            "ldap_migrated": True,
            "migrated_prefixes": ["AUTH_LDAP_"],
        }

    monkeypatch.setattr(
        settings_importer, "_migrate_all_authentication_to_gateway", fake_migrate_auth
    )
    monkeypatch.setattr(
        settings_importer,
        "_generate_settings_review_report",
        lambda review_required, sensitive, auth_info: reports.append(
            (review_required, sensitive, auth_info)
        ),
    )

    result = await settings_importer.import_settings(
        [
            {
                "safe_to_copy": {"DEBUG": False, "AUTH_LDAP_SERVER_URI": "ldap://old"},
                "review_required": {"AUTH_LDAP_BIND_DN": "cn=admin", "BASE_URL": "https://old"},
                "sensitive": {"AUTH_LDAP_BIND_PASSWORD": "$encrypted$"},
                "_summary": {"auto_import_percentage": 75},
            }
        ],
        progress_callback=lambda success, failed, skipped: reports.append(
            ("progress", success, failed, skipped)
        ),
    )

    assert result == [
        {
            "safe_imported": 1,
            "review_required": 1,
            "sensitive_requires_manual": 0,
            "report_generated": "SETTINGS-REVIEW-REPORT.md",
            "ldap_migrated_to_gateway": True,
        }
    ]
    assert settings_client.patch_calls == [("settings/all/", {"DEBUG": False})]
    assert reports[0][0] == {"BASE_URL": "https://old"}
    assert reports[-1] == ("progress", 1, 0, 0)
