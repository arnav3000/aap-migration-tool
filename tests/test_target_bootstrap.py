"""Tests for target bootstrap (pre-scan seeding of id_mappings)."""

from __future__ import annotations

import pytest

from aap_migration.migration.target_bootstrap import (
    BootstrapStats,
    bootstrap_mappings_for_type,
)


class FakeState:
    def __init__(self) -> None:
        self.mapped: dict[tuple[str, int], int] = {}
        self.completed: list[tuple[str, int, int]] = []
        self.saved: list[dict] = []

    def is_migrated(self, resource_type, source_id):
        return (resource_type, source_id) in self.mapped

    def get_mapped_id(self, resource_type, source_id):
        return self.mapped.get((resource_type, source_id))

    def save_id_mapping(self, **kwargs):
        self.saved.append(kwargs)
        self.mapped[(kwargs["resource_type"], kwargs["source_id"])] = kwargs["target_id"]

    def mark_completed(self, **kwargs):
        self.completed.append((kwargs["resource_type"], kwargs["source_id"], kwargs["target_id"]))
        self.mapped[(kwargs["resource_type"], kwargs["source_id"])] = kwargs["target_id"]


class FakeClient:
    def __init__(self, by_type: dict[str, list[dict]]):
        self.by_type = by_type

    async def list_resources(self, resource_type, page_size=200):
        return list(self.by_type.get(resource_type, []))


@pytest.mark.asyncio
async def test_bootstrap_maps_matching_organizations() -> None:
    state = FakeState()
    source = FakeClient(
        {
            "organizations": [
                {"id": 1, "name": "Default"},
                {"id": 2, "name": "Missing"},
            ]
        }
    )
    target = FakeClient({"organizations": [{"id": 99, "name": "Default"}]})

    stats = await bootstrap_mappings_for_type(
        "organizations",
        source,
        target,
        state,  # type: ignore[arg-type]
    )

    assert isinstance(stats, BootstrapStats)
    assert stats.mapped == 1
    assert stats.unmatched == 1
    assert state.mapped[("organizations", 1)] == 99
    assert ("organizations", 2) not in state.mapped


@pytest.mark.asyncio
async def test_bootstrap_applies_name_prefix_and_org_scope() -> None:
    state = FakeState()
    state.mapped[("organizations", 5)] = 50
    source = FakeClient(
        {
            "inventories": [
                {"id": 10, "name": "Prod", "organization": 5},
            ]
        }
    )
    target = FakeClient(
        {
            "inventories": [
                {"id": 200, "name": "dev_Prod", "organization": 50},
            ]
        }
    )

    stats = await bootstrap_mappings_for_type(
        "inventories",
        source,
        target,
        state,  # type: ignore[arg-type]
        name_prefix="dev_",
    )

    assert stats.mapped == 1
    assert state.mapped[("inventories", 10)] == 200


@pytest.mark.asyncio
async def test_bootstrap_skips_memberships() -> None:
    state = FakeState()
    source = FakeClient({})
    target = FakeClient({})
    stats = await bootstrap_mappings_for_type(
        "host_inventory_memberships",
        source,
        target,
        state,  # type: ignore[arg-type]
    )
    assert stats.skipped == 1
    assert stats.mapped == 0


@pytest.mark.asyncio
async def test_bootstrap_credentials_composite_key() -> None:
    state = FakeState()
    state.mapped[("organizations", 1)] = 11
    state.mapped[("credential_types", 2)] = 22
    source = FakeClient(
        {
            "credentials": [
                {
                    "id": 7,
                    "name": "scm",
                    "organization": 1,
                    "credential_type": 2,
                }
            ]
        }
    )
    target = FakeClient(
        {
            "credentials": [
                {
                    "id": 70,
                    "name": "scm",
                    "organization": 11,
                    "credential_type": 22,
                }
            ]
        }
    )

    stats = await bootstrap_mappings_for_type(
        "credentials",
        source,
        target,
        state,  # type: ignore[arg-type]
    )
    assert stats.mapped == 1
    assert state.mapped[("credentials", 7)] == 70
