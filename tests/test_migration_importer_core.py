import pytest

from aap_migration.client.exceptions import APIError, ConflictError
from aap_migration.config import PerformanceConfig
from aap_migration.migration.importer import (
    CredentialImporter,
    CredentialTypeImporter,
    InstanceImporter,
    ProjectImporter,
    ResourceImporter,
    UserImporter,
    create_importer,
    wait_for_project_sync,
)


class FakeState:
    def __init__(self, mapped_ids=None, migrated=None):
        self.database_url = "sqlite:///fake.db"
        self.mapped_ids = dict(mapped_ids or {})
        self.migrated = set(migrated or set())
        self.in_progress = []
        self.completed = []
        self.failed = []
        self.skipped = []
        self.saved = []

    def is_migrated(self, resource_type, source_id):
        return (resource_type, source_id) in self.migrated

    def mark_in_progress(self, **kwargs):
        self.in_progress.append(kwargs)

    def mark_completed(self, **kwargs):
        self.completed.append(kwargs)
        self.migrated.add((kwargs["resource_type"], kwargs["source_id"]))
        if "target_id" in kwargs and kwargs["target_id"] is not None:
            self.mapped_ids[(kwargs["resource_type"], kwargs["source_id"])] = kwargs["target_id"]

    def mark_failed(self, **kwargs):
        self.failed.append(kwargs)

    def mark_skipped(self, **kwargs):
        self.skipped.append(kwargs)

    def save_id_mapping(self, **kwargs):
        self.saved.append(kwargs)
        self.mapped_ids[(kwargs["resource_type"], kwargs["source_id"])] = kwargs["target_id"]

    def get_mapped_id(self, resource_type, source_id):
        return self.mapped_ids.get((resource_type, source_id))


class FakeTargetClient:
    def __init__(self):
        self.find_results = {}
        self.create_result = {"id": 500}
        self.create_error = None
        self.update_result = {"id": 600, "name": "updated"}
        self.update_calls = []
        self.get_results = {}
        self.get_calls = []
        self.post_calls = []
        self.post_result = {"id": 700}

    async def find_resource_by_name(
        self, resource_type, name, organization_id=None, parent_id=None, parent_field=None
    ):
        key = (resource_type, name, organization_id, parent_id, parent_field)
        result = self.find_results.get(key)
        if isinstance(result, Exception):
            raise result
        return result

    async def create_resource(self, resource_type, data, check_exists=True):
        if self.create_error:
            raise self.create_error
        return dict(self.create_result, name=data.get("name"), username=data.get("username"))

    async def update_resource(self, resource_type, target_id, data):
        self.update_calls.append((resource_type, target_id, dict(data)))
        return dict(
            self.update_result, id=target_id, name=data.get("name", self.update_result["name"])
        )

    async def get(self, endpoint, params=None):
        params = params or {}
        self.get_calls.append((endpoint, dict(params)))
        key = (endpoint, tuple(sorted(params.items())))
        result = self.get_results[key]
        if isinstance(result, Exception):
            raise result
        return result

    async def list_resources(self, resource_type, **kwargs):
        return list(self.get_results.get(("list", resource_type), []))

    async def post(self, endpoint, json_data=None):
        self.post_calls.append((endpoint, dict(json_data or {})))
        return dict(self.post_result)


class WidgetImporter(ResourceImporter):
    DEPENDENCIES = {"organization": "organizations", "inventory": "inventories"}


@pytest.mark.asyncio
async def test_resource_importer_helpers_and_base_flow(monkeypatch):
    state = FakeState(mapped_ids={("organizations", 1): 101})
    client = FakeTargetClient()
    importer = WidgetImporter(client, state, PerformanceConfig())

    assert importer._infer_resource_type_from_field("inventory") == "inventories"
    assert importer._infer_resource_type_from_field("unknown") is None

    monkeypatch.setattr(
        importer, "_get_dependency_name", lambda resource_type, source_id: "Main Inv"
    )
    enriched = importer._enrich_api_error_message(
        APIError(
            "bad request",
            response={"inventory": ['Invalid pk "777" - object does not exist.']},
        ),
        "hosts",
        {"inventory": 777},
    )
    assert "Main Inv" in enriched

    resolved = await importer._resolve_dependencies(
        "inventories",
        {"name": "Inventory", "organization": 1, "inventory": 999},
    )
    assert resolved["organization"] == 101
    assert "inventory" not in resolved
    assert importer.unresolved_dependencies[0]["missing_source_id"] == 999

    async def fake_import_resource(resource_type, source_id, data, resolve_dependencies=True):
        if source_id == 1:
            return {"id": 1}
        if source_id == 2:
            state.migrated.add((resource_type, source_id))
            return None
        raise RuntimeError("parallel explode")

    monkeypatch.setattr(importer, "import_resource", fake_import_resource)
    progress = []
    results = await importer._import_parallel(
        "labels",
        [
            {"_source_id": 1, "name": "good"},
            {"_source_id": 2, "name": "skipped"},
            {"_source_id": 3, "name": "boom"},
        ],
        progress_callback=lambda success, failed, skipped: progress.append(
            (success, failed, skipped)
        ),
        concurrency=2,
    )
    assert results == [{"id": 1}]
    assert progress[-1] == (1, 1, 0)
    assert importer.import_errors[-1]["source_id"] == 3


@pytest.mark.asyncio
async def test_resource_importer_import_resource_handles_duplicates_conflicts_and_errors(
    monkeypatch,
):
    state = FakeState()
    client = FakeTargetClient()
    importer = WidgetImporter(client, state, PerformanceConfig())

    missing_org = await importer.import_resource(
        "projects",
        1,
        {"name": "Project Without Org", "organization": None},
        resolve_dependencies=False,
    )
    assert missing_org is None
    assert state.failed[-1]["resource_type"] == "projects"

    client.find_results[("teams", "Team A", 5, None, None)] = {"id": 900, "name": "Team A"}
    duplicate = await importer.import_resource(
        "teams",
        2,
        {"name": "Team A", "organization": 5},
        resolve_dependencies=False,
    )
    assert duplicate["id"] == 900
    assert duplicate["_already_migrated"] is True
    assert state.completed[-1]["target_id"] == 900
    assert state.get_mapped_id("teams", 2) == 900
    assert importer.stats["skipped_count"] == 1

    async def fake_handle_conflict(resource_type, source_id, data):
        return {"id": 901, "name": data["name"]}

    monkeypatch.setattr(importer, "_handle_conflict", fake_handle_conflict)
    client.create_error = ConflictError("exists", status_code=409)
    conflict = await importer.import_resource(
        "labels",
        3,
        {"name": "Label One"},
        resolve_dependencies=False,
    )
    assert conflict["id"] == 901
    assert importer.stats["conflict_count"] == 1

    client.create_error = APIError(
        "validation failed",
        response={"inventory": ['Invalid pk "42" - object does not exist.']},
    )
    monkeypatch.setattr(
        importer, "_get_dependency_name", lambda resource_type, source_id: "Primary Inv"
    )
    api_failure = await importer.import_resource(
        "hosts",
        4,
        {"name": "Host", "inventory": 42},
        resolve_dependencies=False,
    )
    assert api_failure is None
    assert "Primary Inv" in state.failed[-1]["error_message"]

    client.create_error = RuntimeError("totally broken")
    generic_failure = await importer.import_resource(
        "labels",
        5,
        {"name": "Broken Label"},
        resolve_dependencies=False,
    )
    assert generic_failure is None
    assert importer.import_errors[-1]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_specialized_importers_cover_credential_type_user_and_credential_paths():
    performance = PerformanceConfig()

    ct_state = FakeState(mapped_ids={("organizations", 7): 107})
    ct_client = FakeTargetClient()
    ct_client.get_results[("credential_types/", (("name", "Machine"),))] = {
        "results": [{"id": 11, "managed": True}]
    }
    ct_importer = CredentialTypeImporter(ct_client, ct_state, performance)
    managed = await ct_importer.import_resource(
        "credential_types",
        1,
        {"name": "Machine", "organization": 7},
    )
    assert managed["_skipped"] is True
    assert ct_state.saved[-1]["target_id"] == 11

    ct_client.get_results[("credential_types/", (("name", "External Secret"),))] = {"results": []}
    external = await ct_importer.import_resource(
        "credential_types",
        2,
        {"name": "External Secret", "kind": "external"},
    )
    assert external is None

    create_client = FakeTargetClient()
    create_client.get_results[("credential_types/", (("name", "Custom Type"),))] = {"results": []}
    create_importer_instance = CredentialTypeImporter(create_client, FakeState(), performance)
    created = await create_importer_instance.import_resource(
        "credential_types",
        3,
        {"name": "Custom Type", "description": "custom"},
    )
    assert created["id"] == 500

    user_client = FakeTargetClient()
    user_importer = UserImporter(user_client, FakeState(), performance)
    user = await user_importer.import_resource(
        "users",
        10,
        {
            "username": "alice",
            "password": "ignored",
            "ldap_dn": "cn=alice",
            "is_superuser": True,
        },
    )
    assert user["username"] == "alice"

    credential_state = FakeState(
        mapped_ids={
            ("organizations", 7): 107,
            ("credential_types", 30): 330,
            ("users", 9): 109,
        }
    )
    credential_client = FakeTargetClient()
    credential_client.get_results[
        ("credentials/", (("credential_type", 330), ("name", "Vault"), ("organization", 107)))
    ] = {"results": [{"id": 44, "managed": True}]}
    credential_importer = CredentialImporter(credential_client, credential_state, performance)

    resolved_credential = await credential_importer._resolve_dependencies(
        "credentials",
        {"name": "Vault", "credential_type": 5, "organization": 7, "user": 9, "team": 999},
    )
    assert resolved_credential["credential_type"] == 5
    assert resolved_credential["organization"] == 107
    assert resolved_credential["user"] == 109
    assert "team" not in resolved_credential
    assert credential_importer._detect_encrypted_fields(
        {"_encrypted_fields": ["password"], "inputs": {"ssh_key_unlock": "$encrypted$"}}
    ) == ["password", "ssh_key_unlock"]

    managed_credential = await credential_importer.import_resource(
        "credentials",
        20,
        {
            "name": "Vault",
            "organization": 7,
            "credential_type": 30,
            "_encrypted_fields": ["password"],
            "_needs_vault_lookup": True,
        },
    )
    assert managed_credential["_skipped"] is True

    create_credential_client = FakeTargetClient()
    create_credential_client.get_results[
        ("credentials/", (("credential_type", 330), ("name", "Create Me"), ("organization", 107)))
    ] = {"results": []}
    create_credential_importer = CredentialImporter(
        create_credential_client,
        credential_state,
        performance,
    )
    created_credential = await create_credential_importer.import_resource(
        "credentials",
        21,
        {"name": "Create Me", "organization": 7, "credential_type": 30},
    )
    assert created_credential["id"] == 500


@pytest.mark.asyncio
async def test_credential_importer_defaults_owner_to_admin_when_missing():
    performance = PerformanceConfig()
    state = FakeState(mapped_ids={("credential_types", 30): 330})
    client = FakeTargetClient()
    client.get_results[("users/", (("username", "admin"),))] = {
        "results": [{"id": 1, "username": "admin"}]
    }
    # Lookup by name+type with no org (owner will be admin user)
    client.get_results[("credentials/", (("credential_type", 330), ("name", "Orphan Cred")))] = {
        "results": []
    }
    importer = CredentialImporter(client, state, performance)

    created = await importer.import_resource(
        "credentials",
        50,
        {"name": "Orphan Cred", "credential_type": 30},
    )

    assert created["id"] == 500
    # create_resource should have been called with user=admin
    assert ("users/", {"username": "admin"}) in client.get_calls


@pytest.mark.asyncio
async def test_team_importer_reports_unmapped_organization_clearly():
    from aap_migration.migration.importer import TeamImporter

    performance = PerformanceConfig()
    state = FakeState()  # no org mapping
    client = FakeTargetClient()
    importer = TeamImporter(client, state, performance)
    result = await importer.import_resource(
        "teams",
        25,
        {"name": "Ops", "organization": 5},
    )
    assert result is None
    assert "source id 5" in importer.import_errors[-1]["error"]
    assert importer.import_errors[-1]["error_type"] == "ValidationError"


@pytest.mark.asyncio
async def test_instance_importer_maps_by_hostname_instead_of_create():
    performance = PerformanceConfig()
    state = FakeState()
    client = FakeTargetClient()
    client.get_results[("list", "instances")] = [
        {"id": 9, "hostname": "controller.example.com"},
    ]
    importer = InstanceImporter(client, state, performance, resource_mappings={})

    mapped = await importer.import_resource(
        "instances",
        1,
        {"id": 1, "hostname": "controller.example.com"},
    )
    assert mapped["id"] == 9
    assert state.get_mapped_id("instances", 1) == 9
    assert client.create_error is None


@pytest.mark.asyncio
async def test_user_importer_maps_existing_username_on_400():
    performance = PerformanceConfig()
    state = FakeState()
    client = FakeTargetClient()
    client.create_error = APIError(
        "already exists",
        status_code=400,
        response={"username": ["A user with that username already exists."]},
    )
    client.get_results[("users/", (("username", "alice"),))] = {
        "results": [{"id": 42, "username": "alice"}]
    }
    importer = UserImporter(client, state, performance)

    result = await importer.import_resource(
        "users",
        10,
        {"username": "alice", "is_superuser": False},
    )
    assert result["id"] == 42
    assert state.get_mapped_id("users", 10) == 42
    assert importer.stats["conflict_count"] == 1


@pytest.mark.asyncio
async def test_project_importer_attaches_credential_on_already_migrated():
    """Re-runs must PATCH SCM credential onto projects created without one."""
    state = FakeState(
        mapped_ids={
            ("projects", 1): 101,
            ("organizations", 7): 107,
            ("credentials", 9): 209,
        },
        migrated={("projects", 1)},
    )
    client = FakeTargetClient()
    client.get_results[("projects/101/", ())] = {
        "id": 101,
        "name": "dev_Project",
        "credential": None,
    }
    importer = ProjectImporter(client, state, PerformanceConfig())

    result = await importer.import_resource(
        "projects",
        1,
        {
            "name": "dev_Project",
            "organization": 7,
            "credential": 9,
            "scm_type": "git",
            "scm_url": "https://example.com/repo.git",
        },
    )

    assert result is not None
    assert result["_already_migrated"] is True
    assert "credential attached" in result["_skip_reason"]
    assert client.update_calls == [("projects", 101, {"credential": 209})]


@pytest.mark.asyncio
async def test_project_importer_schedule_followup_wait_and_factory(monkeypatch):
    state = FakeState(mapped_ids={("projects", 1): 101})
    client = FakeTargetClient()
    importer = ProjectImporter(client, state, PerformanceConfig())

    async def fake_import_parallel(
        resource_type, resources, progress_callback=None, concurrency=None
    ):
        assert resource_type == "projects"
        return [{"id": 101, "name": "Project One"}]

    monkeypatch.setattr(importer, "_import_parallel", fake_import_parallel)

    projects = [
        {
            "_source_id": 1,
            "name": "Project One",
            "schedules": [
                {"id": 200, "name": "Nightly", "enabled": True, "unified_job_template": 1}
            ],
        }
    ]
    results = await importer.import_projects(projects)
    assert results == [{"id": 101, "name": "Project One"}]
    assert client.post_calls == [("projects/101/schedules/", {"name": "Nightly", "enabled": False})]
    assert state.saved[-1]["resource_type"] == "schedules"

    statuses = {
        "projects/101/": [
            {"id": 101, "name": "Project One", "scm_type": "git", "status": "running"},
            {"id": 101, "name": "Project One", "scm_type": "git", "status": "successful"},
        ],
        "projects/102/": [
            {"id": 102, "name": "Manual", "scm_type": "", "status": "never"},
        ],
        "projects/103/": [
            {"id": 103, "name": "Broken", "scm_type": "git", "status": "failed"},
        ],
    }

    class SyncClient:
        async def get(self, endpoint):
            return statuses[endpoint].pop(0)

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr("aap_migration.migration.importer.asyncio.sleep", fake_sleep)
    progress = []
    synced, failed, failed_ids = await wait_for_project_sync(
        SyncClient(),
        [101, 102, 103],
        timeout=1,
        poll_interval=0,
        progress_callback=lambda success, failures, skipped: progress.append(
            (success, failures, skipped)
        ),
    )
    assert (synced, failed, sorted(failed_ids)) == (2, 1, [103])
    assert progress[-1] == (2, 1, 0)

    assert isinstance(
        create_importer("projects", client, state, PerformanceConfig()), ProjectImporter
    )
    with pytest.raises(NotImplementedError):
        create_importer("unknown", client, state, PerformanceConfig())
