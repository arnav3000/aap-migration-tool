import json
from pathlib import Path

import pytest

from aap_migration.config import MigrationConfig
from aap_migration.migration.transformer import (
    ApplicationTransformer,
    CredentialTransformer,
    CredentialTypeTransformer,
    DataTransformer,
    InstanceGroupTransformer,
    InventoryTransformer,
    JobsTransformer,
    JobTemplateTransformer,
    ProjectTransformer,
    ScheduleTransformer,
    SkipResourceError,
    SystemJobTemplateTransformer,
    WorkflowTransformer,
    coerce_source_id,
    create_transformer,
    generate_temp_encrypted_ssh_key,
    generate_temp_ssh_key,
)


class FakeState:
    def __init__(self, source_mappings=None, mapped_ids=None, mapping_info=None):
        self.source_mappings = set(source_mappings or set())
        self.mapped_ids = dict(mapped_ids or {})
        self.mapping_info = dict(mapping_info or {})
        self.created = []
        self.saved = []
        self.completed = []

    def has_source_mapping(self, resource_type, source_id):
        return (resource_type, source_id) in self.source_mappings

    def create_source_mapping(self, resource_type, source_id, source_name=None):
        self.created.append((resource_type, source_id, source_name))
        self.source_mappings.add((resource_type, source_id))

    def get_id_mapping(self, resource_type, source_id):
        return self.mapping_info.get((resource_type, source_id))

    def get_mapped_id(self, resource_type, source_id):
        return self.mapped_ids.get((resource_type, source_id))

    def save_id_mapping(self, resource_type, source_id, target_id, source_name=None):
        self.saved.append((resource_type, source_id, target_id, source_name))
        self.mapped_ids[(resource_type, source_id)] = target_id

    def mark_completed(
        self,
        resource_type,
        source_id,
        target_id,
        target_name=None,
        source_name=None,
    ):
        self.completed.append(
            {
                "resource_type": resource_type,
                "source_id": source_id,
                "target_id": target_id,
                "target_name": target_name,
                "source_name": source_name,
            }
        )


class FakeTargetClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def get(self, endpoint, params=None):
        params = params or {}
        key = (endpoint, tuple(sorted(params.items())))
        self.calls.append(key)
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        return response


def build_config(resource_mappings=None) -> MigrationConfig:
    return MigrationConfig(
        source={"url": "https://source.example.com", "token": "src-token"},
        target={"url": "https://target.example.com", "token": "dst-token"},
        resource_mappings=resource_mappings or {},
    )


def write_schema(tmp_path: Path, payload: dict) -> Path:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(payload))
    return schema_path


def test_generate_keys_and_base_transformer_schema_flow(tmp_path):
    unencrypted = generate_temp_ssh_key()
    encrypted = generate_temp_encrypted_ssh_key("passphrase")
    private_key_marker = "BEGIN RSA" + " PRIVATE KEY"
    assert private_key_marker in unencrypted
    assert private_key_marker in encrypted
    assert "ENCRYPTED" in encrypted
    assert coerce_source_id({"_source_id": "7"}) == 7
    assert coerce_source_id({"id": 3}) == 3
    assert coerce_source_id({}) == -1

    schema_path = write_schema(
        tmp_path,
        {
            "transformations": {
                "widgets": {
                    "fields_renamed": {
                        "old_name": {
                            "auto_fixable": True,
                            "new_name": "new_name",
                            "confidence": "high",
                        },
                        "legacy": "modern",
                    },
                    "fields_removed": ["deprecated_field"],
                    "new_required_defaults": {"required_flag": True},
                }
            }
        },
    )
    state = FakeState(source_mappings={("organizations", 10)})

    class WidgetTransformer(DataTransformer):
        DEPENDENCIES = {"organization": "organizations", "team": "teams"}
        REQUIRED_DEPENDENCIES = {"organization"}
        REQUIRED_FIELDS = {"widgets": {"name": ""}}

    transformer = WidgetTransformer(schema_comparison_file=schema_path, state=state)

    transformed = transformer.transform_resource(
        "widgets",
        {
            "id": 55,
            "name": "Widget",
            "organization": 10,
            "team": 999,
            "old_name": "before",
            "legacy": "legacy-value",
            "deprecated_field": "drop-me",
            "created": "yesterday",
        },
    )

    assert transformed["new_name"] == "before"
    assert transformed["modern"] == "legacy-value"
    assert transformed["required_flag"] is True
    assert "id" not in transformed
    assert "created" not in transformed
    assert "deprecated_field" not in transformed
    assert state.created == [("widgets", 55, "Widget")]
    assert transformer.stats["transformed_count"] == 1
    assert transformer.stats["fields_removed"] >= 2
    assert transformer.stats["fields_added"] == 1
    assert transformer.stats["fields_renamed"] == 2

    with pytest.raises(SkipResourceError, match="references non-exported organizations 404"):
        transformer.transform_resource(
            "widgets",
            {"id": 56, "name": "Missing Org", "organization": 404},
        )


def test_inventory_and_alias_transformers_handle_normalization_and_mappings():
    state = FakeState(source_mappings={("organizations", 1), ("inventories", 7)})
    inventory_transformer = InventoryTransformer(state=state)

    inventory = inventory_transformer.transform_resource(
        "inventories",
        {"id": 1, "name": "Inventory", "organization": 1, "variables": {"a": 1}},
    )
    assert inventory["kind"] == ""
    assert json.loads(inventory["variables"]) == {"a": 1}

    odd_variables = inventory_transformer.transform_resource(
        "inventories",
        {"id": 2, "name": "Odd", "organization": 1, "variables": ["bad"]},
    )
    assert odd_variables["variables"] == "{}"

    host_transformer = create_transformer("hosts", state=state)
    host_payload = host_transformer.transform_resource(
        "hosts",
        {"id": 3, "name": "host-1", "inventory": 7, "variables": {"hello": "world"}},
    )
    assert json.loads(host_payload["variables"]) == {"hello": "world"}

    group_transformer = create_transformer("groups", state=state)
    group_payload = group_transformer.transform_resource(
        "groups",
        {
            "id": 4,
            "name": "group-1",
            "inventory": 7,
            "variables": None,
            "summary_fields": {"inventory": {"id": 7}},
        },
    )
    assert group_payload["variables"] == "{}"
    assert group_transformer.__class__.__name__ == "InventoryGroupTransformer"
    assert create_transformer("unknown").__class__.__name__ == "DataTransformer"


def test_credential_transformer_handles_external_types_recovery_and_encrypted_fields(
    tmp_path, monkeypatch
):
    cred_types_dir = tmp_path / "credential_types"
    cred_types_dir.mkdir()
    (cred_types_dir / "credential_types_0001.json").write_text(
        json.dumps(
            [
                {"id": 99, "kind": "external", "managed": False},
                {"id": 100, "kind": "external", "managed": True},
            ]
        )
    )

    state = FakeState(
        source_mappings={("organizations", 7), ("credential_types", 18)},
        mapping_info={("credential_types", 18): {"source_name": "HashiCorp Vault Secret Lookup"}},
    )

    monkeypatch.setattr(
        "aap_migration.migration.transformer.generate_temp_ssh_key",
        lambda: "UNENCRYPTED-SSH-KEY",
    )
    monkeypatch.setattr(
        "aap_migration.migration.transformer.generate_temp_encrypted_ssh_key",
        lambda passphrase: f"ENCRYPTED::{passphrase}",
    )
    monkeypatch.setattr(
        "aap_migration.migration.transformer.secrets.token_urlsafe",
        lambda _n: "temp-secret",
    )

    transformer = CredentialTransformer(
        state=state,
        input_dir=tmp_path,
        config=build_config(),
    )

    transformed = transformer.transform_resource(
        "credentials",
        {
            "id": 1,
            "name": "Vault Cred",
            "organization": 7,
            "summary_fields": {"credential_type": {"name": "HashiCorp Vault Secret Lookup"}},
            "related": {"credential_type": "/api/v2/credential_types/18/"},
            "inputs": {
                "ssh_key_unlock": "$encrypted$",
                "ssh_private_key": "$encrypted$",
                "token": "$encrypted$",
                "api_version": "v1",
                "username": "user1",
            },
        },
    )

    assert transformed["credential_type"] == 18
    assert transformed["_needs_vault_lookup"] is True
    assert transformed["_encrypted_fields"] == ["ssh_key_unlock", "ssh_private_key", "token"]
    assert transformed["inputs"]["ssh_key_unlock"] == "temp-secret"
    assert transformed["inputs"]["ssh_private_key"] == "ENCRYPTED::temp-secret"
    assert transformed["inputs"]["token"] == "temp-secret"
    assert "api_version" not in transformed["inputs"]
    assert "username" not in transformed["inputs"]

    defaulted = transformer._apply_specific_transformations(
        {
            "id": 2,
            "name": "Null Org",
            "organization": None,
            "credential_type": 18,
            "inputs": {},
        },
        "credentials",
    )
    assert defaulted["organization"] == 1

    with pytest.raises(SkipResourceError, match="references non-exported credential_types 99"):
        transformer.transform_resource(
            "credentials",
            {
                "id": 3,
                "name": "External Dep",
                "organization": 7,
                "credential_type": 99,
                "inputs": {},
            },
        )


def test_job_project_and_workflow_transformers_normalize_complex_fields():
    state = FakeState(source_mappings={("organizations", 1), ("projects", 2)})

    job_transformer = JobTemplateTransformer(state=state)
    job_payload = job_transformer.transform_resource(
        "job_templates",
        {
            "id": 10,
            "name": "JT",
            "organization": 1,
            "project": 2,
            "custom_virtualenv": "/venv",
            "summary_fields": {
                "credentials": [
                    {
                        "id": 44,
                        "name": "Machine",
                        "description": "desc",
                        "kind": "ssh",
                        "cloud": False,
                    }
                ]
            },
            "_credentials": [999],
            "allow_simultaneous": "yes",
            "ask_inventory_on_launch": "false",
            "webhook_credential": "",
            "webhook_service": "github",
            "webhook_url": "https://hooks.example.com",
            "enable_webhook": True,
            "survey_spec": {
                "spec": [
                    {
                        "type": "password",
                        "default": "$encrypted$",
                        "variable": "pw",
                        "question_name": "Password",
                    }
                ]
            },
        },
    )
    assert job_payload["_needs_execution_environment"] is True
    assert job_payload["_custom_virtualenv_path"] == "/venv"
    assert job_payload["credentials"][0]["id"] == 44
    assert job_payload["allow_simultaneous"] is True
    assert job_payload["ask_inventory_on_launch"] is False
    assert "webhook_credential" not in job_payload
    assert job_payload["survey_spec"]["spec"][0]["default"] == ""

    project_transformer = ProjectTransformer(state=state, defer_project_sync=True)
    project_payload = project_transformer.transform_resource(
        "projects",
        {
            "id": 11,
            "name": "Project",
            "organization": 1,
            "scm_type": "git",
            "scm_url": "https://git.example.com/repo.git",
            "scm_branch": "main",
            "scm_clean": True,
            "scm_delete_on_update": False,
            "scm_update_on_launch": 0,
            "scm_update_cache_timeout": "bad-value",
            "credential": 9,
            "summary_fields": {
                "default_environment": None,
                "credential": {"id": 9},
            },
        },
    )
    assert project_payload["_deferred_scm_details"]["scm_type"] == "git"
    assert project_payload["_deferred_scm_details"]["credential"] == 9
    assert project_payload["_deferred_scm_details"]["scm_update_cache_timeout"] == 0
    assert project_payload["scm_type"] == ""
    assert project_payload["scm_url"] == ""
    assert "scm_branch" not in project_payload
    assert "credential" not in project_payload

    workflow_transformer = WorkflowTransformer(state=state)
    workflow_payload = workflow_transformer.transform_resource(
        "workflow_job_templates",
        {
            "id": 12,
            "name": "Workflow",
            "organization": 1,
            "nodes": [{"id": 200, "identifier": "start"}],
            "ask_variables_on_launch": "yes",
            "survey_enabled": "false",
            "webhook_credential": "",
            "webhook_service": "gitlab",
            "enable_webhook": True,
            "survey_spec": {
                "spec": [
                    {
                        "type": "password",
                        "default": "$encrypted$",
                        "variable": "pw",
                        "question_name": "Password",
                    }
                ]
            },
        },
    )
    assert workflow_payload["_workflow_nodes"] == [{"identifier": "start", "_source_id": 200}]
    assert workflow_payload["ask_variables_on_launch"] is True
    assert workflow_payload["survey_enabled"] is False
    assert "webhook_credential" not in workflow_payload
    assert workflow_payload["survey_spec"]["spec"][0]["default"] == ""


@pytest.mark.asyncio
async def test_credential_type_and_system_job_transformers_populate_target_ids():
    config = build_config(
        resource_mappings={"credential_types": {"CyberArk Legacy": "CyberArk Modern"}}
    )
    state = FakeState()
    target_client = FakeTargetClient(
        responses={
            ("credential_types/", (("name", "CyberArk Modern"),)): {"results": [{"id": 501}]},
            ("system_job_templates/", (("name", "Cleanup"),)): {"results": [{"id": 888}]},
        }
    )

    credential_type_transformer = CredentialTypeTransformer(config=config, state=state)
    transformed = credential_type_transformer.transform_resource(
        "credential_types",
        {
            "id": 30,
            "name": "CyberArk Legacy",
            "managed": True,
            "inputs": {"metadata": {"internal": True}, "fields": []},
        },
    )
    assert transformed["_is_builtin"] is True
    assert transformed["name"] == "CyberArk Modern"
    assert "metadata" not in transformed["inputs"]

    await credential_type_transformer.populate_target_id_from_target(
        transformed,
        target_client,
        state,
        30,
    )
    assert state.saved == [("credential_types", 30, 501, "CyberArk Modern")]
    assert state.completed[0]["target_id"] == 501

    custom_type = {"id": 31, "name": "Custom External", "managed": False}
    returned = await credential_type_transformer.populate_target_id_from_target(
        custom_type,
        target_client,
        state,
        31,
    )
    assert returned is custom_type
    assert credential_type_transformer.stats["skipped_count"] >= 1

    system_transformer = SystemJobTemplateTransformer(state=state)
    await system_transformer.populate_target_id_from_target(
        {"id": 40, "name": "Cleanup"},
        target_client,
        state,
        40,
    )
    assert state.completed[-1]["resource_type"] == "system_job_templates"
    assert state.completed[-1]["target_id"] == 888


def test_schedule_misc_transformers_and_factory_helpers():
    schedule_state = FakeState(
        source_mappings={("job_templates", 14), ("projects", 8), ("organizations", 1)}
    )
    base_transformer = DataTransformer()
    schedule_transformer = ScheduleTransformer(state=schedule_state)

    schedule_payload = {
        "id": 50,
        "name": "Daily Run",
        "unified_job_template": 14,
        "summary_fields": {"unified_job_template": {"type": "job_template"}},
    }
    transformed_schedule = schedule_transformer.transform_resource("schedules", schedule_payload)
    assert transformed_schedule["_ujt_resource_type"] == "job_templates"

    url_schedule = {
        "id": 51,
        "name": "Project Schedule",
        "unified_job_template": 8,
        "related": {"unified_job_template": "/api/v2/projects/8/"},
    }
    schedule_transformer._validate_dependencies(url_schedule, "schedules")
    assert url_schedule["_ujt_resource_type"] == "projects"

    with pytest.raises(SkipResourceError, match="unknown unified_job_template type"):
        schedule_transformer.transform_resource(
            "schedules",
            {"id": 52, "name": "Broken", "unified_job_template": 999},
        )

    base_credentials = base_transformer._transform_credentials(
        {
            "id": 53,
            "summary_fields": {
                "organization": {"id": 1},
                "credential_type": {"id": 18},
            },
            "inputs": {"secret": "$encrypted$", "visible": "value"},
        }
    )
    assert base_credentials["organization"] == 1
    assert base_credentials["credential_type"] == 18
    assert base_credentials["_encrypted_fields"] == ["secret"]
    assert base_credentials["inputs"] == {"visible": "value"}

    parsed_inventory = base_transformer._transform_inventories(
        {"id": 54, "name": "Base Inventory", "variables": '{"good": true}'}
    )
    assert parsed_inventory["variables"] == {"good": True}
    bad_inventory = base_transformer._transform_inventories(
        {"id": 55, "name": "Broken Inventory", "variables": "not-json"}
    )
    assert bad_inventory["variables"] == {}

    group_summary = base_transformer._transform_inventory_groups(
        {"id": 56, "name": "Group", "summary_fields": {"inventory": {"id": 7}}}
    )
    assert group_summary["inventory"] == 7

    base_job_template = base_transformer._transform_job_templates(
        {"id": 57, "name": "JT", "custom_virtualenv": "/venv"}
    )
    assert base_job_template["custom_virtualenv"] == "/venv"

    base_project = base_transformer._transform_projects(
        {
            "id": 58,
            "name": "Project Summary",
            "summary_fields": {
                "organization": {"id": 1},
                "default_environment": None,
                "credential": {"id": 9},
            },
        }
    )
    assert base_project["organization"] == 1
    assert base_project["default_environment"] is None
    assert base_project["credential"] == 9

    inventory_source = base_transformer._transform_inventory_sources(
        {
            "id": 59,
            "name": "Inventory Source",
            "summary_fields": {
                "inventory": {"id": 2},
                "source_project": {"id": 3},
                "credential": {"id": 4},
                "execution_environment": {"id": 5},
            },
        }
    )
    assert inventory_source["inventory"] == 2
    assert inventory_source["source_project"] == 3
    assert inventory_source["credential"] == 4
    assert inventory_source["execution_environment"] == 5

    execution_environment = base_transformer._transform_execution_environments(
        {
            "id": 60,
            "name": "EE",
            "summary_fields": {
                "organization": {"id": 1},
                "credential": {"id": 6},
            },
        }
    )
    assert execution_environment["organization"] == 1
    assert execution_environment["credential"] == 6

    jobs_transformer = JobsTransformer()
    job_record = jobs_transformer.transform_resource(
        "jobs",
        {
            "id": 60,
            "name": "Job 1",
            "summary_fields": {
                "job_template": {"id": 10, "name": "JT"},
                "inventory": {"id": 20, "name": "Inv"},
                "project": {"id": 30, "name": "Proj"},
                "organization": {"id": 40, "name": "Org"},
                "execution_environment": {"id": 50, "name": "EE"},
                "instance_group": {"id": 70, "name": "IG"},
                "launched_by": {"id": 80, "name": "admin", "type": "user"},
            },
        },
    )
    assert job_record["_job_template_name"] == "JT"
    assert job_record["_inventory_id"] == 20
    assert job_record["_launched_by_type"] == "user"

    app_transformer = ApplicationTransformer(state=schedule_state)
    app_payload = app_transformer.transform_resource(
        "applications",
        {"id": 61, "name": "App", "organization": 1, "client_secret": "super-secret"},
    )
    assert app_payload["client_secret"] == "***REDACTED_WILL_BE_REGENERATED***"
    assert app_payload["_requires_new_secret"] is True
    assert app_payload["_migration_notes"]["external_systems_action"] == (
        "update_with_new_client_id_secret"
    )

    settings_payload = create_transformer("settings").transform_resource(
        "settings",
        {
            "_migration_metadata": {"source": "test"},
            "UI_LANDING_PAGE": "jobs",
            "CONTROLLER_URL_BASE": "https://controller.example.com",
            "GITHUB_TOKEN": "masked",
        },
    )
    assert settings_payload["safe_to_copy"]["UI_LANDING_PAGE"] == "jobs"
    assert settings_payload["review_required"]["CONTROLLER_URL_BASE"]["source_value"] == (
        "https://controller.example.com"
    )
    assert settings_payload["sensitive"]["GITHUB_TOKEN"]["_original_value_redacted"] is True
    assert settings_payload["_summary"]["total_settings"] == 3

    instance_group_transformer = InstanceGroupTransformer(
        config=build_config(resource_mappings={"instances": {"old-a": "new-a"}})
    )
    instance_group_payload = instance_group_transformer.transform_resource(
        "instance_groups",
        {"id": 70, "name": "IG", "policy_instance_list": ["old-a", "keep-b"]},
    )
    assert instance_group_payload["policy_instance_list"] == ["new-a", "keep-b"]
