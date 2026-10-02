"""Resource importers by domain (split from migration.importer).

Domain modules:

- :mod:`base` -- :class:`ResourceImporter` + shared constants
- :mod:`identity` -- labels, credential types, users, teams, organizations
- :mod:`inventory_infra` -- instances, instance groups, inventories, sources
- :mod:`scheduling` -- schedules, workflow nodes
- :mod:`execution` -- execution environments, RBAC, hosts, memberships
- :mod:`projects` -- credentials, projects (+ ``wait_for_project_sync``)
- :mod:`templates` -- job templates, workflows
- :mod:`catalog` -- notifications, system job templates, input sources, apps
- :mod:`settings` -- global system settings (SettingsImporter)

``migration.importer`` remains as a re-export shim so existing imports keep
working unchanged.
"""

from __future__ import annotations

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.config import PerformanceConfig
from aap_migration.migration.importers.base import (
    ORGANIZATION_REQUIRED_RESOURCES,
    ORGANIZATION_SCOPED_RESOURCES,
    ResourceImporter,
)
from aap_migration.migration.importers.catalog import (
    ApplicationImporter,
    CredentialInputSourceImporter,
    NotificationTemplateImporter,
    SystemJobTemplateImporter,
)
from aap_migration.migration.importers.execution import (
    ExecutionEnvironmentImporter,
    HostGroupMembershipImporter,
    HostImporter,
    HostInventoryMembershipImporter,
    RBACImporter,
)
from aap_migration.migration.importers.identity import (
    CredentialTypeImporter,
    LabelImporter,
    OrganizationImporter,
    TeamImporter,
    UserImporter,
)
from aap_migration.migration.importers.inventory_infra import (
    InstanceGroupImporter,
    InstanceImporter,
    InventoryGroupImporter,
    InventoryImporter,
    InventorySourceImporter,
)
from aap_migration.migration.importers.projects import (
    CredentialImporter,
    ProjectImporter,
    wait_for_project_sync,
)
from aap_migration.migration.importers.scheduling import (
    ScheduleImporter,
    WorkflowNodeImporter,
)
from aap_migration.migration.importers.settings import SettingsImporter
from aap_migration.migration.importers.templates import (
    JobTemplateImporter,
    WorkflowImporter,
)
from aap_migration.migration.state import MigrationState

__all__ = [
    "ORGANIZATION_REQUIRED_RESOURCES",
    "ORGANIZATION_SCOPED_RESOURCES",
    "ApplicationImporter",
    "CredentialImporter",
    "CredentialInputSourceImporter",
    "CredentialTypeImporter",
    "ExecutionEnvironmentImporter",
    "HostGroupMembershipImporter",
    "HostImporter",
    "HostInventoryMembershipImporter",
    "InstanceGroupImporter",
    "InstanceImporter",
    "InventoryGroupImporter",
    "InventoryImporter",
    "InventorySourceImporter",
    "JobTemplateImporter",
    "LabelImporter",
    "NotificationTemplateImporter",
    "OrganizationImporter",
    "ProjectImporter",
    "RBACImporter",
    "ResourceImporter",
    "ScheduleImporter",
    "SettingsImporter",
    "SystemJobTemplateImporter",
    "TeamImporter",
    "UserImporter",
    "WorkflowImporter",
    "WorkflowNodeImporter",
    "create_importer",
    "wait_for_project_sync",
]


def create_importer(
    resource_type: str,
    client: AAPTargetClient,
    state: MigrationState,
    performance_config: PerformanceConfig,
    resource_mappings: dict[str, dict[str, str]] | None = None,
) -> ResourceImporter:
    """Create appropriate importer for resource type.

    Args:
        resource_type: Type of resource to import
        client: AAP target client instance
        state: Migration state manager
        performance_config: Performance configuration
        resource_mappings: Optional resource name mappings from config/mappings.yaml

    Returns:
        Appropriate ResourceImporter subclass instance

    Raises:
        ValueError: If resource_type is not supported
    """
    importers = {
        # Foundation resources
        "organizations": OrganizationImporter,
        "labels": LabelImporter,
        "instances": InstanceImporter,
        "instance_groups": InstanceGroupImporter,
        # Identity and access
        "users": UserImporter,
        "teams": TeamImporter,
        # Credentials
        "credential_types": CredentialTypeImporter,
        "credentials": CredentialImporter,
        "credential_input_sources": CredentialInputSourceImporter,
        # Projects and execution
        "projects": ProjectImporter,
        "execution_environments": ExecutionEnvironmentImporter,
        # Inventory resources
        "inventories": InventoryImporter,
        "inventory_sources": InventorySourceImporter,
        "inventory_groups": InventoryGroupImporter,
        "hosts": HostImporter,
        "host_inventory_memberships": HostInventoryMembershipImporter,
        "host_group_memberships": HostGroupMembershipImporter,
        # Job templates and workflows
        "job_templates": JobTemplateImporter,
        "workflow_job_templates": WorkflowImporter,
        "schedules": ScheduleImporter,
        # Notifications
        "notification_templates": NotificationTemplateImporter,
        # RBAC
        "rbac": RBACImporter,
        # System
        "system_job_templates": SystemJobTemplateImporter,
        # OAuth and Configuration
        "applications": ApplicationImporter,
        "settings": SettingsImporter,
    }

    importer_class = importers.get(resource_type)
    if not importer_class:
        raise NotImplementedError(
            f"No importer implemented for resource type: {resource_type}. "
            f"Available importers: {', '.join(sorted(importers.keys()))}"
        )

    return importer_class(client, state, performance_config, resource_mappings)
