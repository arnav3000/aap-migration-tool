"""Unit tests for P2 #22 (canonical registry) and P2 #24 (shim removal).

- ``RESOURCE_SPECS`` is the single source of truth: factory lookup, granular
  default steps, and ``ORGANIZATION_*`` sets all derive from it.
- The 4-hop re-export shims are deleted; leaf classes import directly.
"""

import importlib
import os
from typing import Any, cast

from aap_migration.migration import importer as compat_shim
from aap_migration.migration.importers import (
    ORGANIZATION_REQUIRED_RESOURCES,
    ORGANIZATION_SCOPED_RESOURCES,
    RESOURCE_SPECS,
    ResourceImporter,
    create_importer,
)
from aap_migration.migration.importers import _base_helpers as helpers
from aap_migration.migration.importers._registry import (
    DEFAULT_GRANULAR_STEPS,
    get_ordered_resource_types,
)

LEGACY_SCOPED = {
    "teams",
    "projects",
    "inventories",
    "credentials",
    "job_templates",
    "workflow_job_templates",
    "notification_templates",
    "execution_environments",
    "labels",
}
LEGACY_REQUIRED = {
    "teams",
    "projects",
    "inventories",
    "notification_templates",
}
LEGACY_GRANULAR_STEPS = [
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
]
LEGACY_FACTORY_TYPES = {
    "organizations",
    "labels",
    "instances",
    "instance_groups",
    "users",
    "teams",
    "credential_types",
    "credentials",
    "credential_input_sources",
    "projects",
    "execution_environments",
    "inventories",
    "inventory_sources",
    "inventory_groups",
    "hosts",
    "host_inventory_memberships",
    "host_group_memberships",
    "job_templates",
    "workflow_job_templates",
    "schedules",
    "notification_templates",
    "rbac",
    "system_job_templates",
    "applications",
    "settings",
}


def test_registry_covers_legacy_factory_vocabulary() -> None:
    assert set(RESOURCE_SPECS) == LEGACY_FACTORY_TYPES
    for name, (cls, order, scoped, required) in RESOURCE_SPECS.items():
        assert isinstance(cls, type) and issubclass(cls, ResourceImporter), name
        assert isinstance(order, int), name
        assert scoped == (name in LEGACY_SCOPED), name
        assert required == (name in LEGACY_REQUIRED), name


def test_factory_derived_from_registry() -> None:
    for resource_type in sorted(LEGACY_FACTORY_TYPES):
        importer = create_importer(resource_type, cast(Any, None), cast(Any, None), cast(Any, None))
        assert isinstance(importer, RESOURCE_SPECS[resource_type][0]), resource_type
    try:
        create_importer("nope", cast(Any, None), cast(Any, None), cast(Any, None))
    except NotImplementedError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected NotImplementedError")


def test_org_sets_derived_from_registry() -> None:
    assert ORGANIZATION_SCOPED_RESOURCES == LEGACY_SCOPED
    assert ORGANIZATION_REQUIRED_RESOURCES == LEGACY_REQUIRED
    # Helpers re-export the same objects (no forked vocabulary).
    assert helpers.ORGANIZATION_SCOPED_RESOURCES is ORGANIZATION_SCOPED_RESOURCES
    assert helpers.ORGANIZATION_REQUIRED_RESOURCES is ORGANIZATION_REQUIRED_RESOURCES
    # Compat shim still exposes them.
    assert compat_shim.ORGANIZATION_SCOPED_RESOURCES == LEGACY_SCOPED


def test_granular_steps_sorted_by_registry_order() -> None:
    assert DEFAULT_GRANULAR_STEPS == LEGACY_GRANULAR_STEPS
    orders = [RESOURCE_SPECS[n][1] for n in DEFAULT_GRANULAR_STEPS]
    assert orders == sorted(orders)
    assert set(get_ordered_resource_types()) == LEGACY_FACTORY_TYPES


def test_shim_files_deleted_and_leaf_imports_direct() -> None:
    base = os.path.join("src", "aap_migration", "migration", "importers")
    for dead in ("_inventory.py", "inventory_infra.py", "templates.py", "scheduling.py"):
        assert not os.path.exists(os.path.join(base, dead)), dead
    import aap_migration.migration.importers as pkg

    assert "inventory_infra" not in pkg.__file__
    src = open(pkg.__file__).read()
    assert "inventory_infra import" not in src
    assert "from aap_migration.migration.importers.templates import" not in src
    assert "from aap_migration.migration.importers.scheduling import" not in src
    assert "from aap_migration.migration.importers._inventory import" not in src
    # Deprecated dotted aliases still resolve to the same leaf classes.
    templates = importlib.import_module("aap_migration.migration.importers.templates")
    assert templates.JobTemplateImporter is pkg.JobTemplateImporter
