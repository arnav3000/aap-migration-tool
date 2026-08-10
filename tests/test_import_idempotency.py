"""Real-database idempotency tests for MigrationState + importer."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from aap_migration.config import PerformanceConfig, StateConfig
from aap_migration.migration.importer import ResourceImporter
from aap_migration.migration.state import MigrationState


@pytest.mark.asyncio
async def test_import_organization_twice_creates_once(sqlite_db_url: str) -> None:
    """Importing the same organization twice must not call create_resource again."""
    state = MigrationState(
        StateConfig(db_path=sqlite_db_url),
        migration_id="idempotency-org-test",
    )
    client = AsyncMock()
    client.create_resource = AsyncMock(return_value={"id": 42, "name": "Default"})
    client.find_resource_by_name = AsyncMock(return_value=None)

    importer = ResourceImporter(client, state, PerformanceConfig())
    org_data = {"name": "Default", "description": "Primary org"}

    first = await importer.import_resource("organizations", 1, dict(org_data))
    assert first is not None
    assert first["id"] == 42
    assert client.create_resource.await_count == 1

    second = await importer.import_resource("organizations", 1, dict(org_data))
    assert second is None
    assert client.create_resource.await_count == 1
    assert state.is_migrated("organizations", 1)
    assert state.get_mapped_id("organizations", 1) == 42


@pytest.mark.asyncio
async def test_in_progress_without_target_allows_retry(sqlite_db_url: str) -> None:
    """Bare in_progress rows (no target_id) must not block a retry import."""
    state = MigrationState(
        StateConfig(db_path=sqlite_db_url),
        migration_id="in-progress-retry-test",
    )
    state.mark_in_progress("teams", 10, "Team Ten", phase="import")

    assert state.is_migrated("teams", 10) is False

    client = AsyncMock()
    client.create_resource = AsyncMock(return_value={"id": 100, "name": "Team Ten"})
    client.find_resource_by_name = AsyncMock(return_value=None)

    importer = ResourceImporter(client, state, PerformanceConfig())
    result = await importer.import_resource(
        "teams",
        10,
        {"name": "Team Ten", "organization": 1},
        resolve_dependencies=False,
    )

    assert result is not None
    assert client.create_resource.await_count == 1
    assert state.is_migrated("teams", 10)
