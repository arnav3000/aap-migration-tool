from __future__ import annotations

import json

import pytest
from hvac.exceptions import VaultError as HvacVaultError

from aap_migration.client.exceptions import VaultAuthenticationError, VaultError
from aap_migration.client.vault_client import VaultClient
from aap_migration.config import VaultConfig
from aap_migration.schema.models import ChangeType, ComparisonResult, FieldDiff, Severity
from aap_migration.schema.persistence import (
    get_schema_info,
    load_comparison,
    load_schemas,
    save_schemas,
    schema_files_exist,
)


class FakeVaultClientImpl:
    def __init__(self, url=None, namespace=None):
        self.url = url
        self.namespace = namespace
        self.token = None
        self.storage = {}
        self.adapter = type("Adapter", (), {"close": lambda self: None})()
        self.auth = type(
            "Auth",
            (),
            {
                "approle": type(
                    "AppRole",
                    (),
                    {
                        "login": lambda self, role_id, secret_id: {
                            "auth": {
                                "client_token": "token-1",
                                "lease_duration": 600,
                                "renewable": True,
                            }
                        }
                    },
                )(),
                "token": type(
                    "Token",
                    (),
                    {"renew_self": lambda self: {"auth": {"lease_duration": 900}}},
                )(),
            },
        )()
        self.secrets = type(
            "Secrets",
            (),
            {
                "kv": type(
                    "KV",
                    (),
                    {
                        "v2": type(
                            "V2",
                            (),
                            {
                                "create_or_update_secret": self.create_or_update_secret,
                                "read_secret_version": self.read_secret_version,
                                "delete_secret_versions": self.delete_secret_versions,
                                "delete_metadata_and_all_versions": self.delete_metadata_and_all_versions,
                                "list_secrets": self.list_secrets,
                            },
                        )()
                    },
                )()
            },
        )()

    def is_authenticated(self):
        return bool(self.token)

    def create_or_update_secret(self, path, secret, cas=None):
        self.storage[path] = dict(secret)
        return {"data": {"version": 2 if cas else 1}}

    def read_secret_version(self, path, version=None):
        if path not in self.storage:
            raise HvacVaultError("missing")
        return {"data": {"data": self.storage[path]}}

    def delete_secret_versions(self, path, versions):
        return {"deleted": versions, "path": path}

    def delete_metadata_and_all_versions(self, path):
        self.storage.pop(path, None)
        return {"deleted_all": path}

    def list_secrets(self, path):
        if "empty" in path:
            raise HvacVaultError("404")
        return {"data": {"keys": ["first", "second"]}}


def make_vault_config() -> VaultConfig:
    return VaultConfig(
        url="https://vault.example.com",
        role_id="role",
        secret_id="secret",
        namespace="ns",
        path_prefix="secret/aap",
        token_ttl=600,
    )


def test_vault_client_happy_path_and_batch_helpers(monkeypatch):
    monkeypatch.setattr("aap_migration.client.vault_client.hvac.Client", FakeVaultClientImpl)
    client = VaultClient(make_vault_config())

    assert client._build_secret_path("/creds/app/") == "secret/aap/creds/app"
    assert client.is_authenticated() is True

    written = client.write_secret("creds/app", {"username": "admin"}, cas=1)
    assert written["data"]["version"] == 2
    assert client.read_secret("creds/app") == {"username": "admin"}
    assert client.delete_secret("creds/app", versions=[1]) == {
        "deleted": [1],
        "path": "secret/aap/creds/app",
    }

    client.write_secret("creds/app", {"username": "admin"})
    assert client.list_secrets("creds") == ["first", "second"]
    assert client.list_secrets("empty") == []
    assert client.secret_exists("creds/app") is True
    assert client.secret_exists("missing") is False

    results = client.batch_write_secrets(
        {
            "ok-one": {"a": 1},
            "ok-two": {"b": 2},
        }
    )
    assert results == {"successful": ["ok-one", "ok-two"], "failed": []}

    assert client.validate_credential("ok-one", ["a"]) is True
    assert client.validate_credential("ok-one", ["missing"]) is False

    client._token_expires_at = 0
    client._ensure_authenticated()
    assert client._token_expires_at > 0

    client.close()


def test_vault_client_error_paths(monkeypatch):
    class AuthFailureClient(FakeVaultClientImpl):
        def __init__(self, url=None, namespace=None):
            super().__init__(url=url, namespace=namespace)
            self.auth.approle.login = lambda role_id, secret_id: (_ for _ in ()).throw(
                HvacVaultError("bad auth")
            )

    monkeypatch.setattr("aap_migration.client.vault_client.hvac.Client", AuthFailureClient)
    with pytest.raises(VaultAuthenticationError):
        VaultClient(make_vault_config())

    monkeypatch.setattr("aap_migration.client.vault_client.hvac.Client", FakeVaultClientImpl)
    client = VaultClient(make_vault_config())

    client.client.auth.token.renew_self = lambda: (_ for _ in ()).throw(
        HvacVaultError("renew failed")
    )
    called = {"reauth": 0}
    monkeypatch.setattr(
        client, "_authenticate", lambda: called.__setitem__("reauth", called["reauth"] + 1)
    )
    client._renew_token()
    assert called["reauth"] == 1

    client.client.secrets.kv.v2.create_or_update_secret = lambda path, secret, cas=None: (
        _ for _ in ()
    ).throw(HvacVaultError("write failed"))
    with pytest.raises(VaultError, match="Failed to write secret"):
        client.write_secret("broken", {"x": 1})

    client.client.secrets.kv.v2.read_secret_version = lambda path, version=None: (
        _ for _ in ()
    ).throw(HvacVaultError("read failed"))
    with pytest.raises(VaultError, match="Failed to read secret"):
        client.read_secret("broken")

    client.client.secrets.kv.v2.delete_metadata_and_all_versions = lambda path: (
        _ for _ in ()
    ).throw(HvacVaultError("delete failed"))
    with pytest.raises(VaultError, match="Failed to delete secret"):
        client.delete_secret("broken")

    client.client.secrets.kv.v2.list_secrets = lambda path: (_ for _ in ()).throw(
        HvacVaultError("no list")
    )
    with pytest.raises(VaultError, match="Failed to list secrets"):
        client.list_secrets("broken")


@pytest.mark.asyncio
async def test_schema_persistence_round_trip_and_info(tmp_path):
    comparison = ComparisonResult(
        resource_type="projects",
        source_schema={"name": {"type": "string"}},
        target_schema={
            "name": {"type": "string"},
            "organization": {"type": "integer", "required": True},
        },
        field_diffs=[
            FieldDiff(
                field_name="organization",
                change_type=ChangeType.FIELD_ADDED,
                severity=Severity.HIGH,
                target_value={"required": True, "default": None},
            )
        ],
    )

    created = await save_schemas(
        source_schemas={"projects": {"name": {"type": "string"}}},
        target_schemas={
            "projects": {"name": {"type": "string"}, "organization": {"type": "integer"}}
        },
        comparisons={"projects": comparison},
        output_dir=tmp_path,
        source_url="https://src.example.com",
        target_url="https://dst.example.com",
        source_version="2.4",
        target_version="2.6",
    )

    assert set(created) == {"source_schemas", "target_schemas", "comparison"}
    assert schema_files_exist(tmp_path) is False
    assert load_schemas(tmp_path, version="2.4")["projects"]["name"]["type"] == "string"
    loaded_comparison = load_comparison(created["comparison"])
    assert loaded_comparison["resources"]["projects"]["severity"] == "INFO"

    info = get_schema_info(tmp_path)
    assert info["source_version"] == "2.4"
    assert info["target_version"] == "2.6"

    with pytest.raises(FileNotFoundError, match="Run 'aap-bridge schema generate' first"):
        load_comparison(tmp_path / "missing.json")

    with pytest.raises(FileNotFoundError, match="Run 'aap-bridge schema generate' first"):
        load_schemas(tmp_path / "missing-dir", version="2.3")

    broken_file = tmp_path / "schema_comparison.json"
    broken_file.write_text(
        json.dumps(
            {
                "generated_at": "now",
                "source_version": "2.4",
                "target_version": "2.6",
                "resources": {"projects": {}},
            }
        )
    )
    assert schema_files_exist(tmp_path) is False
