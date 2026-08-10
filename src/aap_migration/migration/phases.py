"""Migration phase definitions aligned with IMPORTER_REGISTRY and get_migration_order.

RBAC is intentionally last per AGENTS.md dependency ordering.
"""

from typing import Any

from aap_migration.migration.importers.registry import IMPORTER_REGISTRY


def _registry_types() -> frozenset[str]:
    return frozenset(IMPORTER_REGISTRY.keys())


def get_coordinator_migration_phases() -> list[dict[str, Any]]:
    """Return grouped migration phases for MigrationCoordinator.

    Phases follow ``get_migration_order()`` while grouping related resource
    families. Every ``IMPORTER_REGISTRY`` entry appears exactly once.
    """
    phases: list[dict[str, Any]] = [
        {
            "name": "settings",
            "description": "Global System Settings",
            "resource_types": ["settings"],
            "batch_size": 1,
        },
        {
            "name": "organizations",
            "description": "Organizations (foundation for most resources)",
            "resource_types": ["organizations"],
            "batch_size": 50,
        },
        {
            "name": "credentials",
            "description": "Credential Types and Credentials (REQUIRED BEFORE OTHER RESOURCES)",
            "resource_types": ["credential_types", "credentials"],
            "batch_size": 50,
            "critical": True,
        },
        {
            "name": "credential_input_sources",
            "description": "Credential Input Sources",
            "resource_types": ["credential_input_sources"],
            "batch_size": 100,
        },
        {
            "name": "identity",
            "description": "Labels, Users, and Teams",
            "resource_types": ["labels", "users", "teams"],
            "batch_size": 100,
        },
        {
            "name": "execution_environments",
            "description": "Execution Environments",
            "resource_types": ["execution_environments"],
            "batch_size": 100,
        },
        {
            "name": "inventories",
            "description": "Inventories (80,000+ expected)",
            "resource_types": ["inventories"],
            "batch_size": 100,
        },
        {
            "name": "hosts",
            "description": "Hosts (using bulk operations)",
            "resource_types": ["hosts"],
            "batch_size": 200,
            "use_bulk": True,
        },
        {
            "name": "host_inventory_memberships",
            "description": "Host-Inventory Memberships",
            "resource_types": ["host_inventory_memberships"],
            "batch_size": 100,
        },
        {
            "name": "instances",
            "description": "Instances (AAP Controller Nodes)",
            "resource_types": ["instances"],
            "batch_size": 50,
        },
        {
            "name": "instance_groups",
            "description": "Instance Groups",
            "resource_types": ["instance_groups"],
            "batch_size": 50,
        },
        {
            "name": "projects",
            "description": "Projects",
            "resource_types": ["projects"],
            "batch_size": 100,
        },
        {
            "name": "applications",
            "description": "OAuth Applications",
            "resource_types": ["applications"],
            "batch_size": 50,
        },
        {
            "name": "inventory_config",
            "description": "Inventory Sources and Groups",
            "resource_types": ["inventory_sources", "inventory_groups"],
            "batch_size": 100,
        },
        {
            "name": "notification_templates",
            "description": "Notification Templates",
            "resource_types": ["notification_templates"],
            "batch_size": 100,
        },
        {
            "name": "job_templates",
            "description": "Job Templates",
            "resource_types": ["job_templates"],
            "batch_size": 100,
        },
        {
            "name": "workflows",
            "description": "Workflow Job Templates",
            "resource_types": ["workflow_job_templates"],
            "batch_size": 50,
        },
        {
            "name": "system_job_templates",
            "description": "System Job Templates",
            "resource_types": ["system_job_templates"],
            "batch_size": 50,
        },
        {
            "name": "schedules",
            "description": "Schedules",
            "resource_types": ["schedules"],
            "batch_size": 100,
        },
        {
            "name": "rbac",
            "description": "RBAC role assignments (last — depends on migrated objects)",
            "resource_types": ["rbac"],
            "batch_size": 100,
        },
    ]

    covered: set[str] = set()
    for phase in phases:
        covered.update(phase["resource_types"])

    missing = set(IMPORTER_REGISTRY.keys()) - covered
    if missing:
        phases.append(
            {
                "name": "additional",
                "description": "Additional resource types",
                "resource_types": sorted(missing),
                "batch_size": 100,
            }
        )

    return phases


MIGRATION_PHASES = get_coordinator_migration_phases()
