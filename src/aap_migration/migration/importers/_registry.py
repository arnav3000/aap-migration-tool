"""Canonical resource-type registry for importers (single source of truth).

Before this module the resource-type vocabulary lived in three places that had
already diverged:

- :func:`create_importer` factory dict in :mod:`importers.__init__`
- granular default steps in :mod:`api.services.etl`
- ``ORGANIZATION_*`` scope sets in :mod:`importers._base_helpers`

``RESOURCE_SPECS`` maps each resource-type name to
``(importer_cls, order, org_scoped, org_required)``. The factory lookup, the
granular default steps (sorted by ``order``), and the ``ORGANIZATION_*`` sets
are all derived from it, so adding a resource type means editing the flags
table and the class table in this file only.

Import cycle note: this module performs *no* leaf imports at top level (the
flags, scope sets, and step list are plain data), so
``base -> _base_helpers -> _registry`` can never deadlock regardless of import
order. Leaf classes are bound lazily by :func:`get_resource_specs` (local
imports, cached); :mod:`importers.__init__` calls it once at package import,
which always precedes any use, so ``RESOURCE_SPECS`` is populated before any
consumer reads it. Service modules (e.g. ``api.services.etl``) import only
the derived step list, never the classes.
"""

from __future__ import annotations

# -- canonical per-resource flags (order, org_scoped, org_required) --------
# ``order`` follows the proven granular migration order; gaps leave room for
# resource kinds that run outside the granular menu (labels, instances, memberships,
# notification/system templates, rbac).
_RESOURCE_FLAGS: dict[str, tuple[int, bool, bool]] = {
    # name: (order, org_scoped, org_required)
    "organizations": (10, False, False),
    "labels": (15, True, False),
    "users": (20, False, False),
    "teams": (30, True, True),
    "credential_types": (40, False, False),
    "credentials": (50, True, False),
    "credential_input_sources": (55, False, False),
    "execution_environments": (60, True, False),
    "projects": (70, True, True),
    "inventories": (80, True, True),
    "inventory_sources": (90, False, False),
    "inventory_groups": (100, False, False),
    "hosts": (110, False, False),
    "host_inventory_memberships": (112, False, False),
    "host_group_memberships": (114, False, False),
    "instances": (115, False, False),
    "instance_groups": (120, False, False),
    "job_templates": (130, True, False),
    "workflow_job_templates": (140, True, False),
    "schedules": (150, False, False),
    "notification_templates": (155, True, True),
    "system_job_templates": (156, False, False),
    "applications": (160, False, False),
    "settings": (170, False, False),
    "rbac": (180, False, False),
}

# -- derived scope sets (single home; re-exported by helpers/__init__) -----
ORGANIZATION_SCOPED_RESOURCES: set[str] = {
    name for name, (_, scoped, _) in _RESOURCE_FLAGS.items() if scoped
}
ORGANIZATION_REQUIRED_RESOURCES: set[str] = {
    name for name, (_, _, required) in _RESOURCE_FLAGS.items() if required
}

# -- granular default steps (menu subset, sorted by canonical order) ---------
# Matches the historical etl.py default exactly; the menu covers the phases
# with dedicated micro-steps, the rest run inside their parent phase.
_GRANULAR_STEP_NAMES: frozenset[str] = frozenset(
    {
        "organizations",
        "users",
        "teams",
        "credential_types",
        "credentials",
        "execution_environments",
        "projects",
        "inventories",
        "inventory_sources",
        "inventory_groups",
        "hosts",
        "instance_groups",
        "job_templates",
        "workflow_job_templates",
        "schedules",
        "applications",
        "settings",
    }
)

DEFAULT_GRANULAR_STEPS: list[str] = sorted(
    _GRANULAR_STEP_NAMES, key=lambda n: _RESOURCE_FLAGS[n][0]
)

# -- canonical spec table (populated lazily; see module docstring) ----------
RESOURCE_SPECS: dict[str, tuple[type, int, bool, bool]] = {}
_specs_built = False


def get_resource_specs() -> dict[str, tuple[type, int, bool, bool]]:
    """Return the canonical spec table, building it once on first use."""
    global _specs_built
    if not _specs_built:
        from aap_migration.migration.importers._instances import (
            InstanceGroupImporter,
            InstanceImporter,
        )
        from aap_migration.migration.importers._inventories import InventoryImporter
        from aap_migration.migration.importers._inventory_groups import (
            InventoryGroupImporter,
            InventorySourceImporter,
        )
        from aap_migration.migration.importers._job_templates import JobTemplateImporter
        from aap_migration.migration.importers._schedules import ScheduleImporter
        from aap_migration.migration.importers._workflows import WorkflowImporter
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
        )
        from aap_migration.migration.importers.settings import SettingsImporter

        classes: dict[str, type] = {
            "organizations": OrganizationImporter,
            "labels": LabelImporter,
            "instances": InstanceImporter,
            "instance_groups": InstanceGroupImporter,
            "users": UserImporter,
            "teams": TeamImporter,
            "credential_types": CredentialTypeImporter,
            "credentials": CredentialImporter,
            "credential_input_sources": CredentialInputSourceImporter,
            "projects": ProjectImporter,
            "execution_environments": ExecutionEnvironmentImporter,
            "inventories": InventoryImporter,
            "inventory_sources": InventorySourceImporter,
            "inventory_groups": InventoryGroupImporter,
            "hosts": HostImporter,
            "host_inventory_memberships": HostInventoryMembershipImporter,
            "host_group_memberships": HostGroupMembershipImporter,
            "job_templates": JobTemplateImporter,
            "workflow_job_templates": WorkflowImporter,
            "schedules": ScheduleImporter,
            "notification_templates": NotificationTemplateImporter,
            "rbac": RBACImporter,
            "system_job_templates": SystemJobTemplateImporter,
            "applications": ApplicationImporter,
            "settings": SettingsImporter,
        }
        flags = set(_RESOURCE_FLAGS)
        if flags != set(classes):  # pragma: no cover - developer error
            raise AssertionError(
                f"registry drift: flags-only={sorted(flags - set(classes))}, "
                f"classes-only={sorted(set(classes) - flags)}"
            )
        unknown_steps = set(_GRANULAR_STEP_NAMES) - flags
        if unknown_steps:  # pragma: no cover - developer error
            raise AssertionError(f"granular steps unknown: {sorted(unknown_steps)}")
        for name, (order, scoped, required) in _RESOURCE_FLAGS.items():
            RESOURCE_SPECS[name] = (classes[name], order, scoped, required)
        _specs_built = True
    return RESOURCE_SPECS


def get_importer_class(resource_type: str) -> type:
    """Return the importer class for *resource_type* (KeyError when unknown)."""
    return get_resource_specs()[resource_type][0]


def get_ordered_resource_types() -> list[str]:
    """Return every known resource type in canonical migration order."""
    return sorted(_RESOURCE_FLAGS, key=lambda n: _RESOURCE_FLAGS[n][0])


__all__ = [
    "DEFAULT_GRANULAR_STEPS",
    "ORGANIZATION_REQUIRED_RESOURCES",
    "ORGANIZATION_SCOPED_RESOURCES",
    "RESOURCE_SPECS",
    "get_importer_class",
    "get_ordered_resource_types",
    "get_resource_specs",
]
