"""Resource importers by domain (split from migration.importer).

Domain modules:

- :mod:`base` -- :class:`ResourceImporter` + shared helpers
- :mod:`identity` -- labels, credential types, users, teams, organizations
- :mod:`_instances` -- instances, instance groups
- :mod:`_inventories` -- inventories
- :mod:`_inventory_groups` -- inventory groups, inventory sources
- :mod:`_schedules` -- schedules
- :mod:`_workflow_nodes` -- workflow nodes
- :mod:`execution` -- execution environments, RBAC, hosts, memberships
- :mod:`projects` -- credentials, projects (+ ``wait_for_project_sync``)
- :mod:`_job_templates` -- job templates
- :mod:`_workflows` -- workflows
- :mod:`catalog` -- notifications, system job templates, input sources, apps
- :mod:`settings` -- global system settings (SettingsImporter)

``migration.importer`` remains as the single compat re-export shim so existing
imports keep working unchanged. The removed intermediate shims
(``inventory_infra``, ``_inventory``, ``templates``, ``scheduling``) are kept
importable via deprecated :data:`sys.modules` aliases below (test compat);
new code must import leaf classes (or this package) directly.
"""

from __future__ import annotations

import sys as _sys
import types as _types

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.config import PerformanceConfig
from aap_migration.migration.importers._instances import (
    InstanceGroupImporter,
    InstanceImporter,
)
from aap_migration.migration.importers._inventories import InventoryImporter

# Leaf classes imported directly (no intermediate re-export shims).
from aap_migration.migration.importers._inventory_groups import (
    InventoryGroupImporter,
    InventorySourceImporter,
)
from aap_migration.migration.importers._job_templates import JobTemplateImporter
from aap_migration.migration.importers._registry import (
    ORGANIZATION_REQUIRED_RESOURCES,
    ORGANIZATION_SCOPED_RESOURCES,
    RESOURCE_SPECS,
    get_resource_specs,
)
from aap_migration.migration.importers._schedules import ScheduleImporter
from aap_migration.migration.importers._workflow_nodes import WorkflowNodeImporter
from aap_migration.migration.importers._workflows import WorkflowImporter
from aap_migration.migration.importers.base import ResourceImporter
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
from aap_migration.migration.importers.projects import (
    CredentialImporter,
    ProjectImporter,
    wait_for_project_sync,
)
from aap_migration.migration.importers.settings import SettingsImporter
from aap_migration.migration.state import MigrationState

__all__ = [
    "ORGANIZATION_REQUIRED_RESOURCES",
    "ORGANIZATION_SCOPED_RESOURCES",
    "RESOURCE_SPECS",
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
    "get_resource_specs",
    "wait_for_project_sync",
]


# Populate the canonical spec table now: the parent package always fully
# executes before any consumer reads RESOURCE_SPECS, so the lazy table is
# complete before first use (see _registry module docstring).
get_resource_specs()


def create_importer(
    resource_type: str,
    client: AAPTargetClient,
    state: MigrationState,
    performance_config: PerformanceConfig,
    resource_mappings: dict[str, dict[str, str]] | None = None,
) -> ResourceImporter:
    """Create appropriate importer for resource type.

    The lookup is derived from the canonical
    :data:`importers._registry.RESOURCE_SPECS` table (single source of truth).

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
    spec = RESOURCE_SPECS.get(resource_type)
    if spec is None:
        raise NotImplementedError(
            f"No importer implemented for resource type: {resource_type}. "
            f"Available importers: {', '.join(sorted(RESOURCE_SPECS.keys()))}"
        )
    importer_class: type[ResourceImporter] = spec[0]
    return importer_class(client, state, performance_config, resource_mappings)


def _install_deprecated_shim_aliases() -> None:
    """Expose removed intermediate shims under their old dotted names.

    Deprecated: the shim files (``inventory_infra``, ``_inventory``,
    ``templates``, ``scheduling``) were deleted; :mod:`migration.importer`
    is the single supported compat path. These aliases exist only so
    already-written ``from ...importers.templates import X`` imports
    (e.g. in tests) keep resolving to the same leaf classes.
    """
    _aliases: dict[str, dict[str, object]] = {
        f"{__name__}._inventory": {
            "InventoryImporter": InventoryImporter,
            "InventoryGroupImporter": InventoryGroupImporter,
            "InventorySourceImporter": InventorySourceImporter,
        },
        f"{__name__}.inventory_infra": {
            "InstanceImporter": InstanceImporter,
            "InstanceGroupImporter": InstanceGroupImporter,
            "InventoryImporter": InventoryImporter,
            "InventoryGroupImporter": InventoryGroupImporter,
            "InventorySourceImporter": InventorySourceImporter,
        },
        f"{__name__}.templates": {
            "JobTemplateImporter": JobTemplateImporter,
            "WorkflowImporter": WorkflowImporter,
        },
        f"{__name__}.scheduling": {
            "ScheduleImporter": ScheduleImporter,
            "WorkflowNodeImporter": WorkflowNodeImporter,
        },
    }
    for dotted, attrs in _aliases.items():
        if dotted in _sys.modules:  # pragma: no cover - defensive
            continue
        module = _types.ModuleType(dotted)
        module.__doc__ = "Deprecated alias: import leaf classes directly."
        for key, value in attrs.items():
            setattr(module, key, value)
        module.__dict__["__all__"] = sorted(attrs)
        _sys.modules[dotted] = module


_install_deprecated_shim_aliases()
