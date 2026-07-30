from __future__ import annotations

import pytest

from aap_migration.client.exceptions import ConflictError
from aap_migration.config import StateConfig
from aap_migration.migration.state import MigrationState
from aap_migration.utils.idempotency import (
    compare_resources,
    deduplicate_list,
    find_existing_resource,
    generate_resource_key,
    handle_conflict,
    hash_resource,
    idempotent,
    is_duplicate,
)


def build_state(tmp_path) -> MigrationState:
    return MigrationState(
        StateConfig(db_path=str(tmp_path / "state.db")),
        migration_id="migration-1",
        migration_name="Example Migration",
    )


def test_resource_key_hash_and_duplicate_helpers() -> None:
    resource = {"name": "host-1", "inventory": {"id": 42}, "created": "yesterday"}
    same = {"inventory": {"id": 42}, "name": "host-1", "created": "today"}

    assert (
        generate_resource_key(resource, ["name", "inventory.id"]) == "name:host-1|inventory.id:42"
    )
    assert generate_resource_key({"name": "x"}, ["name", "inventory.id"]) == "name:x|inventory.id:"
    assert hash_resource(resource, exclude_fields=["created"]) == hash_resource(
        same, exclude_fields=["created"]
    )
    assert compare_resources(resource, same) is True
    assert is_duplicate(resource, [same], ["name", "inventory.id"]) is True
    assert deduplicate_list([resource, same], ["name", "inventory.id"]) == [resource]


@pytest.mark.asyncio
async def test_idempotent_decorator_marks_completed_and_returns_cached_result(tmp_path) -> None:
    state = build_state(tmp_path)
    calls: list[dict[str, object]] = []

    @idempotent(
        state, "inventories", ["name"], source_id_field="source_id", source_name_field="name"
    )
    async def create_inventory(data: dict[str, object]) -> dict[str, object]:
        calls.append(data)
        return {"id": 101, "name": data["name"]}

    first = await create_inventory({"source_id": 5, "name": "Default"})
    second = await create_inventory({"source_id": 5, "name": "Default"})

    assert first == {"id": 101, "name": "Default"}
    assert second["id"] == 101
    assert calls == [{"source_id": 5, "name": "Default"}]
    assert state.get_mapped_id("inventories", 5) == 101


@pytest.mark.asyncio
async def test_idempotent_decorator_handles_conflicts_with_existing_resource(tmp_path) -> None:
    state = build_state(tmp_path)

    class FakeClient:
        async def find_resource_by_name(self, resource_type: str, name: str, organization=None):
            return {"id": 202, "name": name}

    @idempotent(state, "projects", ["name"], source_id_field="source_id", source_name_field="name")
    async def create_project(data: dict[str, object], *, client) -> dict[str, object]:
        raise ConflictError("already exists", status_code=409)

    result = await create_project({"source_id": 9, "name": "Website"}, client=FakeClient())

    assert result == {"id": 202, "name": "Website"}
    assert state.get_mapped_id("projects", 9) == 202


@pytest.mark.asyncio
async def test_idempotent_decorator_raises_for_missing_ids_and_unhandled_conflicts(
    tmp_path,
) -> None:
    state = build_state(tmp_path)

    @idempotent(
        state, "organizations", ["name"], source_id_field="source_id", source_name_field="name"
    )
    async def bad_result(data: dict[str, object]) -> dict[str, object]:
        return {"id": None}

    with pytest.raises(TypeError, match="requires numeric target id"):
        await bad_result({"source_id": 1, "name": "Default"})

    @idempotent(
        state, "organizations", ["name"], source_id_field="source_id", source_name_field="name"
    )
    async def conflict_without_client(data: dict[str, object]) -> dict[str, object]:
        raise ConflictError("still broken", status_code=409)

    with pytest.raises(ConflictError):
        await conflict_without_client({"source_id": 2, "name": "Default"})


@pytest.mark.asyncio
async def test_conflict_helpers_find_existing_and_raise_when_missing() -> None:
    class FakeClient:
        def __init__(self, result):
            self.result = result

        async def find_resource_by_name(self, resource_type: str, name: str, organization=None):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    existing = await find_existing_resource(
        FakeClient({"id": 7, "name": "Demo"}),
        "inventories",
        {"name": "Demo", "organization": 1},
    )
    missing_name = await find_existing_resource(FakeClient({"id": 1}), "inventories", {})
    failure = await find_existing_resource(
        FakeClient(RuntimeError("boom")), "inventories", {"name": "Demo"}
    )

    assert existing == {"id": 7, "name": "Demo"}
    assert missing_name is None
    assert failure is None

    with pytest.raises(ConflictError, match="Could not find existing resource after conflict"):
        await handle_conflict(
            FakeClient(None),
            "inventories",
            {"name": "Demo"},
            ConflictError("already exists", status_code=409),
        )

    handled = await handle_conflict(
        FakeClient({"id": 8, "name": "Demo"}),
        "inventories",
        {"name": "Demo"},
        ConflictError("already exists", status_code=409),
    )
    assert handled == {"id": 8, "name": "Demo"}
