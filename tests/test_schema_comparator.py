from __future__ import annotations

import pytest

from aap_migration.client.aap_source_client import AAPSourceClient
from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.config import AAPInstanceConfig
from aap_migration.schema.comparator import SchemaComparator
from aap_migration.schema.models import ChangeType


def make_source_client() -> AAPSourceClient:
    return AAPSourceClient(AAPInstanceConfig(url="https://source.example.com", token="token"))


def make_target_client() -> AAPTargetClient:
    return AAPTargetClient(AAPInstanceConfig(url="https://target.example.com", token="token"))


@pytest.mark.asyncio
async def test_fetch_schema_extracts_23_and_26_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    comparator = SchemaComparator()
    source = make_source_client()
    target = make_target_client()

    async def source_request(**kwargs):
        return {"actions": {"POST": {"name": {"type": "string", "required": True}}}}

    async def target_request(**kwargs):
        return {"name": {"type": "string", "required": True}}

    monkeypatch.setattr(source, "request", source_request)
    monkeypatch.setattr(target, "request", target_request)

    try:
        assert await comparator.fetch_schema(source, "organizations") == {
            "name": {"type": "string", "required": True}
        }
        assert await comparator.fetch_schema(target, "organizations") == {
            "name": {"type": "string", "required": True}
        }
    finally:
        await source.close()
        await target.close()


def test_compare_schemas_detects_added_removed_type_and_validation_changes() -> None:
    comparator = SchemaComparator()
    source_schema = {
        "id": {"type": "integer"},
        "custom_virtualenv": {"type": "string", "required": False},
        "description": {"type": "string", "required": False},
        "credential_kind": {"type": "string", "required": False},
    }
    target_schema = {
        "description": {"type": "textarea", "required": False},
        "execution_environment": {"type": "string", "required": True, "default": "ee-1"},
        "organization": {"type": "field", "required": True},
    }

    result = comparator.compare_schemas("job_templates", source_schema, target_schema)

    assert result.has_changes is True
    assert result.has_breaking_changes is True
    assert set(result.deprecated_fields) == {"custom_virtualenv", "credential_kind"}
    assert result.new_required_fields == {
        "execution_environment": "ee-1",
        "organization": None,
    }
    assert result.type_changes == {"description": ("string", "textarea")}
    assert any(diff.change_type == ChangeType.FIELD_ADDED for diff in result.field_diffs)
    assert any(
        change.change_type == ChangeType.VALIDATION_CHANGED for change in result.schema_changes
    )

    summary = result.get_summary()
    serialized = result.to_dict()

    assert summary["resource_type"] == "job_templates"
    assert serialized["severity"] in {"HIGH", "MEDIUM", "LOW", "CRITICAL", "INFO"}
    assert serialized["auto_fixable"] is False


def test_detect_field_renames_and_generate_rules() -> None:
    comparator = SchemaComparator()
    source_schema = {
        "user_name": {"type": "string", "required": True},
        "old_field": {"type": "integer", "required": False},
    }
    target_schema = {
        "username": {"type": "string", "required": True},
        "new_field": {"type": "integer", "required": False},
    }

    renames = comparator.detect_field_renames(
        source_schema,
        target_schema,
        removed_fields={"user_name", "old_field"},
        added_fields={"username", "new_field"},
    )

    assert renames["user_name"].new_name == "username"
    assert renames["user_name"].auto_fixable is True
    assert "similar_name" in renames["user_name"].reason
    assert (
        comparator._calculate_rename_score(
            "user_name",
            {"type": "string"},
            "username",
            {"type": "string"},
        )
        > 0.6
    )
    assert (
        comparator._get_rename_reason(
            "old",
            {"type": "integer", "required": True},
            "new",
            {"type": "integer", "required": True},
            0.75,
        )
        == "same_type_and_same_required_status"
    )

    comparison = comparator.compare_schemas(
        "credentials",
        {"name": {"type": "string"}},
        {"name": {"type": "string"}, "organization": {"type": "field", "required": True}},
    )
    comparison.field_renames = renames

    assert comparator.generate_transformation_rules(comparison) == {
        "resource_type": "credentials",
        "fields_to_remove": [],
        "fields_to_add": {"organization": None},
        "type_conversions": {},
        "has_breaking_changes": True,
    }
