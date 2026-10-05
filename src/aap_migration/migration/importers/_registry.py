"""Canonical resource-type registry for importers (single source of truth).

Before this module the resource-type vocabulary lived in three places that had
already diverged:

- :func:`create_importer` factory dict in :mod:`importers.__init__`
- granular default steps in :mod:`cli.granular_import` (``MICRO_PHASES``)
- ``ORGANIZATION_*`` scope sets in :mod:`importers._base_helpers`

``RESOURCE_SPECS`` maps each resource-type name to a :class:`ResourceSpec`
``(importer_cls, order, org_scoped, org_required)``. The factory lookup, the
granular default steps (sorted by ``order``), and the ``ORGANIZATION_*`` sets
are all derived from it, so adding a resource type means editing the flags
table and the class table in this file only.

The table is built lazily on first :func:`get_resource_specs` use (leaf
imports live inside ``_build_specs``): importing this module must never import
leaf modules, or the ``base -> _base_helpers -> _registry`` chain deadlocks
(an eager build at import time was tried and reverted for exactly this
cycle). The drift check is therefore a fail-closed whole-run gate at first
use, not per-type containment: a flags-vs-classes typo raises
``AssertionError`` on the first factory call, before any importer is built,
and aborts the run. Service modules (e.g. ``cli.granular_import``) keep their
own menu; keep it in sync via the parity test in
``tests/unit/test_importer_factory.py``.

Import cycle note: the flags, scope sets, and step list are plain data defined
before any leaf import, so ``base -> _base_helpers -> _registry`` can never
deadlock regardless of import order: those modules only need the data names.
Consumers must resolve through :func:`get_resource_specs` rather than reading
or mutating the exposed dict: it is empty until the first accessor call, it is
built once and never healed, so in-process mutation persists (fix test
pollution with fixtures, not heal). Use :func:`reset_registry_for_tests`
in tests to restore a clean built state.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from aap_migration.migration.importers.base import ResourceImporter


class ResourceFlags(NamedTuple):
    """Canonical per-resource flags: migration order plus org scoping."""

    order: int
    org_scoped: bool
    org_required: bool


class ResourceSpec(NamedTuple):
    """Canonical per-resource spec: importer class plus its flags."""

    importer_cls: type[ResourceImporter]
    order: int
    org_scoped: bool
    org_required: bool


# -- canonical per-resource flags (order, org_scoped, org_required) --------
# ``order`` follows the proven granular migration order; gaps leave room for
# resource kinds that run outside the granular menu (labels, instances, memberships,
# notification/system templates, rbac).
_RESOURCE_FLAGS: dict[str, ResourceFlags] = {
    # name: ResourceFlags(order, org_scoped, org_required)
    "organizations": ResourceFlags(10, False, False),
    "labels": ResourceFlags(15, True, False),
    "users": ResourceFlags(20, False, False),
    "teams": ResourceFlags(30, True, True),
    "credential_types": ResourceFlags(40, False, False),
    "credentials": ResourceFlags(50, True, False),
    "credential_input_sources": ResourceFlags(55, False, False),
    "execution_environments": ResourceFlags(60, True, False),
    "projects": ResourceFlags(70, True, True),
    "inventories": ResourceFlags(80, True, True),
    "inventory_sources": ResourceFlags(90, False, False),
    "inventory_groups": ResourceFlags(100, False, False),
    "hosts": ResourceFlags(110, False, False),
    "host_inventory_memberships": ResourceFlags(112, False, False),
    "host_group_memberships": ResourceFlags(114, False, False),
    "instances": ResourceFlags(115, False, False),
    "instance_groups": ResourceFlags(120, False, False),
    "job_templates": ResourceFlags(130, True, False),
    "workflow_job_templates": ResourceFlags(140, True, False),
    "schedules": ResourceFlags(150, False, False),
    "notification_templates": ResourceFlags(155, True, True),
    "system_job_templates": ResourceFlags(156, False, False),
    "applications": ResourceFlags(160, False, False),
    "settings": ResourceFlags(170, False, False),
    "rbac": ResourceFlags(180, False, False),
}

# -- derived scope sets (single home; re-exported by helpers/__init__) -----
ORGANIZATION_SCOPED_RESOURCES: set[str] = {
    name for name, flags in _RESOURCE_FLAGS.items() if flags.org_scoped
}
ORGANIZATION_REQUIRED_RESOURCES: set[str] = {
    name for name, flags in _RESOURCE_FLAGS.items() if flags.org_required
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
    _GRANULAR_STEP_NAMES, key=lambda n: _RESOURCE_FLAGS[n].order
)

# -- canonical spec table (built lazily on first use; see _build_specs) -----
# Empty until the first get_resource_specs() call: leaf imports live inside the
# builder so importing this module never triggers the
# base -> _base_helpers -> _registry cycle. Built once; never healed
# (in-process mutation persists, so tests must use snapshot/restore fixtures
# or reset_registry_for_tests()). Read via get_resource_specs(), never
# directly at import time.
RESOURCE_SPECS: dict[str, ResourceSpec] = {}
_specs_built = False
_specs_lock = threading.Lock()


def _build_specs() -> None:
    """Populate :data:`RESOURCE_SPECS` once, on first use.

    Fail-closed: any flags-vs-classes skew raises ``AssertionError`` here and
    aborts the run on first factory use, before any importer is built. This is
    deliberate -- a half-built registry must never serve partial results, and
    a lazy per-type fallback would let a typo'd type fail far from its cause.
    DEPENDENCIES values are validated against the same vocabulary: a leaf
    typo naming an unknown type raises ``AssertionError`` here instead of
    silently pruning the filtered-import closure at runtime.
    """
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

    classes: dict[str, type[ResourceImporter]] = {
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
    if flags != set(classes):
        raise AssertionError(
            f"registry drift: flags-only={sorted(flags - set(classes))}, "
            f"classes-only={sorted(set(classes) - flags)}"
        )
    unknown_steps = set(_GRANULAR_STEP_NAMES) - flags
    if unknown_steps:
        raise AssertionError(f"granular steps unknown: {sorted(unknown_steps)}")
    for dep_holder, dep_cls in classes.items():
        holder_deps = getattr(dep_cls, "DEPENDENCIES", {})
        if isinstance(holder_deps, dict):
            unknown_dep_values = sorted({v for v in holder_deps.values() if v not in flags})
            if unknown_dep_values:
                raise AssertionError(
                    f"registry drift: DEPENDENCIES values unknown for "
                    f"'{dep_holder}': {', '.join(unknown_dep_values)}"
                )
    for name, flags_entry in _RESOURCE_FLAGS.items():
        RESOURCE_SPECS[name] = ResourceSpec(
            importer_cls=classes[name],
            order=flags_entry.order,
            org_scoped=flags_entry.org_scoped,
            org_required=flags_entry.org_required,
        )


def get_resource_specs() -> dict[str, ResourceSpec]:
    """Return the canonical spec table, building it once on first use.

    The returned dict is the live table: read it, do not mutate it. Test
    doubles must patch this table (or its entries) -- patching a leaf,
    package, or shim attribute has no effect on :func:`create_importer`,
    which snapshots the class at lookup time from this table.
    """
    global _specs_built
    # Double-checked locking: concurrent first use must not double-build.
    if not _specs_built:
        with _specs_lock:
            if not _specs_built:
                _build_specs()
                _specs_built = True
    return RESOURCE_SPECS


def reset_registry_for_tests() -> dict[str, ResourceSpec]:
    """Rebuild the canonical spec table from scratch (tests only).

    Clears the built flag and table, then rebuilds via the normal
    fail-closed path so drift gates fire. Use in an autouse fixture to
    isolate live-table mutation between tests. Never call in production:
    the registry is built once and never healed.
    """
    global _specs_built
    with _specs_lock:
        RESOURCE_SPECS.clear()
        _specs_built = False
        _build_specs()
        _specs_built = True
    return RESOURCE_SPECS


def get_importer_class(resource_type: str) -> type[ResourceImporter]:
    """Return the importer class for *resource_type* (KeyError when unknown)."""
    return get_resource_specs()[resource_type].importer_cls


def get_ordered_resource_types() -> list[str]:
    """Return every known resource type in canonical migration order."""
    return sorted(_RESOURCE_FLAGS, key=lambda n: _RESOURCE_FLAGS[n].order)


__all__ = [
    "DEFAULT_GRANULAR_STEPS",
    "ORGANIZATION_REQUIRED_RESOURCES",
    "ORGANIZATION_SCOPED_RESOURCES",
    "ResourceFlags",
    "ResourceSpec",
    "get_importer_class",
    "get_ordered_resource_types",
    "get_resource_specs",
    "reset_registry_for_tests",
]
