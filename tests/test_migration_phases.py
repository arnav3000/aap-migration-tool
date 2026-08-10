"""Tests for migration phase ordering (AGENTS.md: RBAC last)."""

from aap_migration.migration.coordinator import MigrationCoordinator
from aap_migration.migration.importers.registry import IMPORTER_REGISTRY
from aap_migration.migration.phases import MIGRATION_PHASES, get_coordinator_migration_phases
from aap_migration.resources import get_migration_order


def test_migration_phases_rbac_last_and_cover_registry():
    phases = get_coordinator_migration_phases()

    assert phases[-1]["name"] == "rbac"
    assert phases[-1]["resource_types"] == ["rbac"]

    flattened: list[str] = []
    for phase in phases:
        flattened.extend(phase["resource_types"])

    assert flattened[-1] == "rbac"
    assert set(flattened) == set(IMPORTER_REGISTRY.keys())
    assert len(flattened) == len(set(flattened))

    registry_order = [rt for rt in get_migration_order() if rt in IMPORTER_REGISTRY]
    for resource_type in registry_order:
        assert resource_type in flattened

    assert MigrationCoordinator.MIGRATION_PHASES == MIGRATION_PHASES
