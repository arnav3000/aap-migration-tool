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
importable via deprecated :data:`sys.modules` aliases below; new code must
import leaf classes (or this package) directly. Alias removal is deferred to
the tip of the current stacked series: later stacks' tests import the
``templates`` path, so deleting the aliases here would break those branches
on rebase (see review on PR 139). Sunset: remove
``_install_deprecated_shim_aliases`` at the tip of the 5-stack series once
later stacks import leaf paths directly; do not extend the alias table.

Domain rule (public vs private): public modules (``base``, ``catalog``,
``execution``, ``identity``, ``projects``, ``settings``) are stable domains;
private ``_``-prefixed modules are leaf details. New importers belong in the
matching public domain, or a private leaf when the domain would breach the
1k-line guard. ``credential`` lives in ``projects`` next to its project
consumer while ``credential_type`` lives in ``identity``; keep that split
unless both move together.
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


# The canonical spec table builds lazily on first create_importer() /
# get_resource_specs() use; see _registry.py for the contract (fail-closed
# drift gate, live-table semantics, mock guidance). This package intentionally
# does not re-export the raw dict: reading it before first use would see an
# empty table.


def create_importer(
    resource_type: str,
    client: AAPTargetClient,
    state: MigrationState,
    performance_config: PerformanceConfig,
    resource_mappings: dict[str, dict[str, str]] | None = None,
) -> ResourceImporter:
    """Create appropriate importer for resource type.

    The lookup is derived from the canonical
    :data:`importers._registry.RESOURCE_SPECS` table (single source of truth),
    read via :func:`get_resource_specs` (built lazily on first use).

    Note on test doubles: the class is snapshotted from the spec table at
    lookup time, so ``mock.patch`` on a leaf, package, or shim attribute does
    not affect the factory once the table is built. Patch the table entry
    itself (``monkeypatch.setitem(get_resource_specs(), ...)``).

    Args:
        resource_type: Type of resource to import
        client: AAP target client instance
        state: Migration state manager
        performance_config: Performance configuration
        resource_mappings: Optional resource name mappings from config/mappings.yaml

    Returns:
        Appropriate ResourceImporter subclass instance

    Raises:
        NotImplementedError: If resource_type is not supported
    """
    specs = get_resource_specs()
    spec = specs.get(resource_type)
    if spec is None:
        raise NotImplementedError(
            f"No importer implemented for resource type: {resource_type}. "
            f"Available importers: {', '.join(sorted(specs.keys()))}"
        )
    importer_class: type[ResourceImporter] = spec.importer_cls
    return importer_class(client, state, performance_config, resource_mappings)


def _install_deprecated_shim_aliases() -> None:
    """Expose removed intermediate shims under their old dotted names.

    Deprecated: the shim files (``inventory_infra``, ``_inventory``,
    ``templates``, ``scheduling``) were deleted; :mod:`migration.importer`
    is the single supported compat path. These aliases exist only so
    already-written ``from ...importers.templates import X`` imports
    keep resolving to the same leaf classes during the migration window.

    New code (including tests) must import leaf classes directly, e.g.
    ``from aap_migration.migration.importers._job_templates import
    JobTemplateImporter``. Likewise, ``mock.patch`` targets must use the
    canonical leaf path — patching the alias (e.g.
    ``...importers.templates.JobTemplateImporter``) only shadows the alias
    while :func:`create_importer` looks up the real class in
    ``RESOURCE_SPECS``.

    Alias reads delegate live to the canonical leaf modules (PEP 562
    ``__getattr__``), so an alias consumer always sees the current class
    object, never a snapshot copy.

    Sunset: remove these aliases at the tip of the 5-stack series (see
    package docstring); both ``from ...templates import X`` and dotted
    ``import ...templates`` forms are supported until then.
    """
    import importlib as _importlib
    import importlib.machinery as _machinery

    _aliases: dict[str, dict[str, tuple[str, str]]] = {
        f"{__name__}._inventory": {
            "InventoryImporter": (f"{__name__}._inventories", "InventoryImporter"),
            "InventoryGroupImporter": (
                f"{__name__}._inventory_groups",
                "InventoryGroupImporter",
            ),
            "InventorySourceImporter": (
                f"{__name__}._inventory_groups",
                "InventorySourceImporter",
            ),
        },
        f"{__name__}.inventory_infra": {
            "InstanceImporter": (f"{__name__}._instances", "InstanceImporter"),
            "InstanceGroupImporter": (
                f"{__name__}._instances",
                "InstanceGroupImporter",
            ),
            "InventoryImporter": (f"{__name__}._inventories", "InventoryImporter"),
            "InventoryGroupImporter": (
                f"{__name__}._inventory_groups",
                "InventoryGroupImporter",
            ),
            "InventorySourceImporter": (
                f"{__name__}._inventory_groups",
                "InventorySourceImporter",
            ),
        },
        f"{__name__}.templates": {
            "JobTemplateImporter": (
                f"{__name__}._job_templates",
                "JobTemplateImporter",
            ),
            "WorkflowImporter": (f"{__name__}._workflows", "WorkflowImporter"),
        },
        f"{__name__}.scheduling": {
            "ScheduleImporter": (f"{__name__}._schedules", "ScheduleImporter"),
            "WorkflowNodeImporter": (
                f"{__name__}._workflow_nodes",
                "WorkflowNodeImporter",
            ),
        },
    }
    for dotted, attrs in _aliases.items():
        if dotted in _sys.modules:  # pragma: no cover - defensive
            continue
        module = _types.ModuleType(dotted)
        module.__doc__ = "Deprecated alias: import leaf classes directly."
        module.__spec__ = _machinery.ModuleSpec(dotted, loader=None)
        module.__loader__ = None
        module.__dict__["__all__"] = sorted(attrs)

        def __getattr__(
            name: str,
            _attrs: dict[str, tuple[str, str]] = attrs,
            _dotted: str = dotted,
        ) -> object:
            try:
                leaf_module_name, leaf_attr = _attrs[name]
            except KeyError:
                raise AttributeError(f"module {_dotted!r} has no attribute {name!r}") from None
            return getattr(_importlib.import_module(leaf_module_name), leaf_attr)

        module.__getattr__ = __getattr__  # type: ignore[method-assign]
        _sys.modules[dotted] = module
        # Bind on the parent package as well so dotted access
        # (import a.b.c; a.b.c) works, not just from-imports and
        # importlib.import_module. Without this, pkg.templates raises
        # AttributeError despite the sys.modules entry.
        parent_name, _, child = dotted.rpartition(".")
        parent = _sys.modules.get(parent_name)
        if parent is not None and not hasattr(parent, child):
            setattr(parent, child, module)


_install_deprecated_shim_aliases()
