from __future__ import annotations

import json

import pytest
from rich.console import Console

from aap_migration.migration.credential_comparator import CredentialComparator
from aap_migration.validation.dependency_validator import DependencyValidator


class FakeState:
    def __init__(self):
        self.mappings = {("organizations", 10): 110, ("credential_types", 20): 220}
        self.saved = []
        self.completed = []
        self.migrated = set()

    def get_mapped_id(self, resource_type, source_id):
        return self.mappings.get((resource_type, source_id))

    def is_migrated(self, resource_type, source_id):
        return (resource_type, source_id) in self.migrated

    def save_id_mapping(self, resource_type, source_id, target_id, source_name=None):
        self.saved.append((resource_type, source_id, target_id, source_name))

    def mark_completed(self, **kwargs):
        self.completed.append(kwargs)


class FakeCredentialClient:
    def __init__(self, pages):
        self.pages = pages

    async def get(self, endpoint, params):
        assert endpoint == "/credentials/"
        return self.pages[params["page"]]


def test_dependency_validator_load_validate_and_display(tmp_path) -> None:
    input_dir = tmp_path / "xformed"
    credentials_dir = input_dir / "credentials"
    credentials_dir.mkdir(parents=True)
    (credentials_dir / "credentials_1.json").write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "name": "db-password",
                    "_source_id": 1,
                    "organization": 10,
                    "credential_type": 20,
                }
            ]
        )
    )
    (credentials_dir / "credentials_2.json").write_text(
        json.dumps(
            {
                "id": 2,
                "name": "ssh-key",
                "_source_id": 2,
                "organization": 99,
                "credential_type": 20,
            }
        )
    )
    (credentials_dir / "credentials_3.json").write_text("{broken")

    validator = DependencyValidator(FakeState(), input_dir)
    validator.console = Console(record=True, width=120)

    loaded = validator.load_resources("credentials")
    assert [item["name"] for item in loaded] == ["db-password", "ssh-key"]

    ready = validator.validate_resource_dependencies(loaded[0], "credentials")
    blocked = validator.validate_resource_dependencies(loaded[1], "credentials")
    assert ready["status"] == "ready"
    assert blocked["status"] == "blocked"
    assert blocked["missing_deps"] == [
        {"field": "organization", "type": "organizations", "source_id": 99}
    ]

    batch = validator.validate_batch("credentials")
    all_results = validator.validate_all(["credentials", "projects"])

    assert batch["ready"] == 1
    assert batch["blocked"] == 1
    assert all_results["overall"]["can_proceed"] is False

    validator.display_validation_report(all_results)
    output = validator.console.export_text()
    assert "Pre-Flight Dependency Validation" in output
    assert "db-password" not in output
    assert "ssh-key" in output
    assert "Cannot proceed" in output


@pytest.mark.asyncio
async def test_credential_comparator_compare_and_report() -> None:
    state = FakeState()
    state.migrated.add(("credentials", 5))

    source_client = FakeCredentialClient(
        {
            1: {
                "results": [
                    {
                        "id": 1,
                        "name": "Machine",
                        "credential_type": 7,
                        "organization": 42,
                        "description": "SSH machine credential",
                        "inputs": {"username": "admin"},
                        "managed": False,
                        "summary_fields": {
                            "credential_type": {"name": "Machine"},
                            "organization": {"name": "Default"},
                        },
                    },
                    {
                        "id": 5,
                        "name": "Vault",
                        "credential_type": 9,
                        "organization": None,
                        "description": "",
                        "inputs": {},
                        "managed": False,
                        "summary_fields": {"credential_type": {"name": "Vault"}},
                    },
                    {
                        "id": 8,
                        "name": "Managed",
                        "credential_type": 9,
                        "organization": None,
                        "description": "",
                        "inputs": {},
                        "managed": True,
                        "summary_fields": {"credential_type": {"name": "Vault"}},
                    },
                ],
                "next": "/api/v2/credentials/?page=2",
            },
            2: {
                "results": [
                    {
                        "id": 11,
                        "name": "TargetOnly",
                        "credential_type": 10,
                        "organization": None,
                        "description": "",
                        "inputs": {},
                        "managed": False,
                    }
                ],
                "next": None,
            },
        }
    )
    target_client = FakeCredentialClient(
        {
            1: {
                "results": [
                    {
                        "id": 51,
                        "name": "Vault",
                        "credential_type": 9,
                        "organization": None,
                        "description": "",
                        "inputs": {},
                        "managed": False,
                    }
                ],
                "next": None,
            }
        }
    )

    comparator = CredentialComparator(source_client, target_client, state)
    result = await comparator.compare_credentials()

    assert result.total_source == 4
    assert result.total_target == 1
    assert result.matching_credentials == 1
    assert result.managed_credentials_skipped == 1
    assert [
        (diff.source_id, diff.name, diff.organization_name) for diff in result.missing_in_target
    ] == [
        (1, "Machine", "Default"),
        (11, "TargetOnly", None),
    ]

    # The non-migrated match stores mapping and marks the state complete.
    assert state.saved == []
    assert state.completed == []

    report = comparator.generate_report(result)
    assert "# Credential Comparison Report" in report
    assert "## Missing Credentials" in report
    assert "Machine" in report
    assert "TargetOnly" in report
    assert "Note: Secret values will need manual entry" in report
