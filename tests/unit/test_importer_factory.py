"""Factory parity: create_importer must cover every base-commit resource type.

Regression test for the domain split dropping the ``schedules`` entry, so
schedule imports raised NotImplementedError (kept on the table-driven parity
test below, which owns that contract).

Also guards the split mechanics the review on PR 139 called out: the spec
table is built lazily and read via the accessor (never re-exported raw), test
doubles patch the table entry (leaf/package/shim patching is ineffective),
the dependency closure derives from the same registry, and moved importer
bodies preserve the non-mutating-read behavior of the base commit.
"""

import copy
import importlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import aap_migration.migration.importers as importers_pkg
from aap_migration.migration import importer as compat_shim
from aap_migration.migration.importers import (
    _registry as registry_module,
)
from aap_migration.migration.importers import (
    create_importer,
    get_resource_specs,
)
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
from aap_migration.migration.importers._registry import (
    DEFAULT_GRANULAR_STEPS,
    ResourceSpec,
    get_importer_class,
    get_ordered_resource_types,
    reset_registry_for_tests,
)
from aap_migration.migration.importers._registry import (
    RESOURCE_SPECS as REGISTRY_SPECS,
)
from aap_migration.migration.importers._schedules import ScheduleImporter
from aap_migration.migration.importers._workflow_nodes import WorkflowNodeImporter
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

EXPECTED_TYPES: dict[str, type] = {
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

# Every exported importer class and the leaf module that defines it.
# WorkflowNodeImporter is intentionally absent from the factory map (it has no
# standalone resource type; workflows drive it), but it must stay importable.
CLASS_MODULES: dict[str, str] = {
    "OrganizationImporter": "aap_migration.migration.importers.identity",
    "LabelImporter": "aap_migration.migration.importers.identity",
    "InstanceImporter": "aap_migration.migration.importers._instances",
    "InstanceGroupImporter": "aap_migration.migration.importers._instances",
    "UserImporter": "aap_migration.migration.importers.identity",
    "TeamImporter": "aap_migration.migration.importers.identity",
    "CredentialTypeImporter": "aap_migration.migration.importers.identity",
    "CredentialImporter": "aap_migration.migration.importers.projects",
    "CredentialInputSourceImporter": "aap_migration.migration.importers.catalog",
    "ProjectImporter": "aap_migration.migration.importers.projects",
    "ExecutionEnvironmentImporter": "aap_migration.migration.importers.execution",
    "InventoryImporter": "aap_migration.migration.importers._inventories",
    "InventorySourceImporter": "aap_migration.migration.importers._inventory_groups",
    "InventoryGroupImporter": "aap_migration.migration.importers._inventory_groups",
    "HostImporter": "aap_migration.migration.importers.execution",
    "HostInventoryMembershipImporter": "aap_migration.migration.importers.execution",
    "HostGroupMembershipImporter": "aap_migration.migration.importers.execution",
    "JobTemplateImporter": "aap_migration.migration.importers._job_templates",
    "WorkflowImporter": "aap_migration.migration.importers._workflows",
    "WorkflowNodeImporter": "aap_migration.migration.importers._workflow_nodes",
    "ScheduleImporter": "aap_migration.migration.importers._schedules",
    "NotificationTemplateImporter": "aap_migration.migration.importers.catalog",
    "RBACImporter": "aap_migration.migration.importers.execution",
    "SystemJobTemplateImporter": "aap_migration.migration.importers.catalog",
    "ApplicationImporter": "aap_migration.migration.importers.catalog",
    "SettingsImporter": "aap_migration.migration.importers.settings",
}

# Full canonical migration order (registry flags order). Pinned exactly so a
# flags typo or reorder fails here rather than silently reshuffling imports.
EXPECTED_ORDER = [
    "organizations",
    "labels",
    "users",
    "teams",
    "credential_types",
    "credentials",
    "credential_input_sources",
    "execution_environments",
    "projects",
    "inventories",
    "inventory_sources",
    "inventory_groups",
    "hosts",
    "host_inventory_memberships",
    "host_group_memberships",
    "instances",
    "instance_groups",
    "job_templates",
    "workflow_job_templates",
    "schedules",
    "notification_templates",
    "system_job_templates",
    "applications",
    "settings",
    "rbac",
]

# Hardcoded granular menu snapshot, independent of both live sources.
# Dropping a type from both the registry and MICRO_PHASES must fail here,
# not stay green on a live-vs-live comparison.
EXPECTED_GRANULAR_STEPS = [
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

# Exact canonical schedule UJT parent tuple. Subset-only checks pass on
# addition and silently over-import; this pins widening and narrowing.
EXPECTED_SCHEDULE_UJT_PARENTS = (
    "job_templates",
    "workflow_job_templates",
    "projects",
    "inventory_sources",
)

# Per-type DEPENDENCIES content snapshot, pinned independently of the live
# registry so a leaf typo fails here even though both sides of the
# registry-vs-closure parity check would otherwise agree on the typo.
EXPECTED_DEPS: dict[str, dict[str, str]] = {
    "applications": {"organization": "organizations"},
    "credential_input_sources": {
        "credential": "credentials",
        "source_credential": "credentials",
    },
    "credential_types": {"organization": "organizations"},
    "credentials": {
        "organization": "organizations",
        "credential_type": "credential_types",
        "user": "users",
        "team": "teams",
    },
    "execution_environments": {
        "organization": "organizations",
        "credential": "credentials",
    },
    "host_group_memberships": {"group_id": "inventory_groups", "host_id": "hosts"},
    "host_inventory_memberships": {"host_id": "hosts", "inventory_id": "inventories"},
    "hosts": {"inventory": "inventories"},
    "instance_groups": {"credential": "credentials"},
    "instances": {},
    "inventories": {"organization": "organizations"},
    "inventory_groups": {"inventory": "inventories", "parent": "inventory_groups"},
    "inventory_sources": {
        "inventory": "inventories",
        "source_project": "projects",
        "credential": "credentials",
        "execution_environment": "execution_environments",
    },
    "job_templates": {
        "organization": "organizations",
        "inventory": "inventories",
        "project": "projects",
        "credential": "credentials",
        "execution_environment": "execution_environments",
        "webhook_credential": "credentials",
    },
    "labels": {"organization": "organizations"},
    "notification_templates": {"organization": "organizations"},
    "organizations": {"default_environment": "execution_environments"},
    "projects": {
        "organization": "organizations",
        "credential": "credentials",
        "default_environment": "execution_environments",
    },
    "rbac": {},
    "schedules": {
        "inventory": "inventories",
        "execution_environment": "execution_environments",
    },
    "settings": {},
    "system_job_templates": {},
    "teams": {"organization": "organizations"},
    "users": {},
    "workflow_job_templates": {
        "organization": "organizations",
        "inventory": "inventories",
        "webhook_credential": "credentials",
    },
}


@pytest.fixture(autouse=True)
def _isolate_live_registry() -> Iterator[None]:
    """Snapshot and restore the live singleton so table mutation in one test
    cannot pollute another under reorder or parallel runs."""
    specs = get_resource_specs()
    snapshot = dict(specs)
    built = registry_module._specs_built
    try:
        yield
    finally:
        specs.clear()
        specs.update(snapshot)
        registry_module._specs_built = built


def test_create_importer_covers_all_base_map_keys() -> None:
    # Bidirectional parity: the factory table must equal the expected
    # vocabulary in both directions so drift fails in either direction.
    # (Owns the historic schedules-drop regression: schedules is asserted
    # with the stronger type() is check below, like every other entry.)
    specs = get_resource_specs()
    assert set(specs.keys()) == set(EXPECTED_TYPES.keys())
    # The accessor returns the live canonical table.
    assert specs is REGISTRY_SPECS
    for resource_type, expected_cls in sorted(EXPECTED_TYPES.items()):
        importer = create_importer(resource_type, None, None, None)
        assert type(importer) is expected_cls, resource_type
    # Spot-check a neighboring entry so a future drop is localized.
    assert isinstance(
        create_importer("job_templates", None, None, None),
        JobTemplateImporter,
    )


def test_create_importer_forwards_args_to_leaf() -> None:
    # The factory must construct the leaf class with exactly the given
    # (client, state, performance_config, resource_mappings).
    # Patch target pin: the effective double point is the spec-table entry
    # (leaf/package/shim attribute patching does NOT affect the factory,
    # which snapshots the class at lookup time).
    client, state, perf = object(), object(), object()
    mappings = {"credentials": {"old": "new"}}

    class Recorder(ScheduleImporter):
        seen: tuple | None = None

        def __init__(self, client, state, performance_config, resource_mappings=None):  # type: ignore[no-untyped-def]
            Recorder.seen = (client, state, performance_config, resource_mappings)

    specs = get_resource_specs()
    original = specs["schedules"]
    specs["schedules"] = ResourceSpec(
        importer_cls=Recorder, order=150, org_scoped=False, org_required=False
    )
    try:
        create_importer("schedules", client, state, perf, mappings)
    finally:
        specs["schedules"] = original
    assert Recorder.seen == (client, state, perf, mappings)

    # resource_mappings=None normalizes to {} as before the split.
    importer = create_importer("schedules", MagicMock(), MagicMock(), MagicMock())
    assert importer.resource_mappings == {}


def test_create_importer_unknown_type_raises() -> None:
    with pytest.raises(NotImplementedError):
        create_importer("bogus-type", None, None, None)


def test_registry_table_built_on_first_use() -> None:
    # No heal: the table builds once via the accessor and is never rebuilt.
    # (No empty-before-use trap either -- the raw dict is not re-exported by
    # the package; only this accessor is public.)
    assert not hasattr(importers_pkg, "RESOURCE_SPECS")
    assert "RESOURCE_SPECS" not in registry_module.__all__
    specs = get_resource_specs()
    assert len(specs) == len(EXPECTED_TYPES) == 25
    assert set(specs.keys()) == set(EXPECTED_TYPES.keys())
    # Named access: positional indexing was the pre-split trap.
    assert specs["schedules"].importer_cls is ScheduleImporter
    assert specs["schedules"].order == 150


def test_registry_empty_before_first_use_and_reset() -> None:
    # Direct reads before the first accessor call see an empty table; the
    # reset helper rebuilds through the normal fail-closed path.
    registry_module.RESOURCE_SPECS.clear()
    registry_module._specs_built = False
    try:
        assert registry_module.RESOURCE_SPECS == {}
        rebuilt = reset_registry_for_tests()
        assert len(rebuilt) == len(EXPECTED_TYPES) == 25
        assert registry_module._specs_built is True
    finally:
        reset_registry_for_tests()


def test_registry_double_build_idempotent() -> None:
    first = get_resource_specs()
    second = get_resource_specs()
    assert first is second
    assert len(second) == 25


def test_compat_shim_reexports_package() -> None:
    for name in compat_shim.__all__:
        assert hasattr(compat_shim, name), name
        assert name in dir(__import__("aap_migration.migration.importers", fromlist=["x"])), name
    # Reverse direction with identity: a name the package exposes must also be
    # re-exported by the shim as the identical object, so a dropped export or
    # an accidentally redefined stub fails. get_resource_specs is new registry
    # API (never previously defined in migration.importer), so it is the
    # documented exception.
    shim_only_new = {"get_resource_specs"}
    for name in importers_pkg.__all__:
        if name in shim_only_new:
            continue
        assert name in compat_shim.__all__, name
        assert getattr(compat_shim, name) is getattr(importers_pkg, name), name


def test_importer_namespace_completeness() -> None:
    # Every importer class must resolve to the identical object from its leaf
    # module, the domain package, and the compat shim: a class dropped or
    # redefined in transit fails here, not in production.
    for name, dotted in sorted(CLASS_MODULES.items()):
        leaf = importlib.import_module(dotted)
        assert getattr(leaf, name) is getattr(importers_pkg, name), name
        assert getattr(compat_shim, name) is getattr(importers_pkg, name), name
    # WorkflowNodeImporter has no standalone resource type (workflows drive
    # it): pin the exclusion so a well-meaning "fix" adding it fails loudly.
    assert "workflow_nodes" not in get_resource_specs()
    assert importers_pkg.WorkflowNodeImporter is WorkflowNodeImporter


def test_deprecated_alias_modules_delegate_to_leaves() -> None:
    # Alias removal is deferred to the tip of the stacked series (later
    # stacks' tests import the templates path); until then every alias entry
    # must resolve live to its leaf class.
    cases: dict[str, dict[str, type]] = {
        "aap_migration.migration.importers.templates": {
            "JobTemplateImporter": JobTemplateImporter,
            "WorkflowImporter": WorkflowImporter,
        },
        "aap_migration.migration.importers.scheduling": {
            "ScheduleImporter": ScheduleImporter,
            "WorkflowNodeImporter": WorkflowNodeImporter,
        },
        "aap_migration.migration.importers.inventory_infra": {
            "InstanceImporter": InstanceImporter,
            "InstanceGroupImporter": InstanceGroupImporter,
            "InventoryImporter": InventoryImporter,
            "InventoryGroupImporter": InventoryGroupImporter,
            "InventorySourceImporter": InventorySourceImporter,
        },
        "aap_migration.migration.importers._inventory": {
            "InventoryImporter": InventoryImporter,
            "InventoryGroupImporter": InventoryGroupImporter,
            "InventorySourceImporter": InventorySourceImporter,
        },
    }
    for dotted, expected in cases.items():
        module = importlib.import_module(dotted)
        assert set(expected) <= set(module.__all__), dotted
        for name, leaf_cls in expected.items():
            assert getattr(module, name) is leaf_cls, f"{dotted}.{name}"
        with pytest.raises(AttributeError):
            getattr(module, "NoSuchImporter")  # noqa: B009 -- must use getattr to hit __getattr__


def test_deprecated_alias_reads_are_live(monkeypatch: pytest.MonkeyPatch) -> None:
    # A snapshot copy would keep serving the old object after the leaf is
    # repatched; the PEP 562 __getattr__ delegation must see the new one.
    sentinel = type("SentinelJobTemplateImporter", (), {})
    monkeypatch.setattr(
        "aap_migration.migration.importers._job_templates.JobTemplateImporter",
        sentinel,
    )
    module = importlib.import_module("aap_migration.migration.importers.templates")
    assert module.JobTemplateImporter is sentinel


def test_deprecated_alias_parent_attribute_dotted_access() -> None:
    # Both import forms must work until sunset: from-imports and dotted
    # access via the parent package (import a.b.c; a.b.c).
    import aap_migration.migration.importers.templates as templates_alias

    # getattr (not dotted access) so static type checkers do not need
    # version-sensitive ignores for the dynamic sys.modules aliases.
    templates_mod = getattr(importers_pkg, "templates")  # noqa: B009 -- dynamic alias
    assert templates_mod is templates_alias
    assert (
        getattr(templates_mod, "JobTemplateImporter")  # noqa: B009 -- dynamic alias
        is JobTemplateImporter
    )
    scheduling_mod = getattr(importers_pkg, "scheduling")  # noqa: B009 -- dynamic alias
    assert (
        getattr(scheduling_mod, "ScheduleImporter")  # noqa: B009 -- dynamic alias
        is ScheduleImporter
    )
    infra_mod = getattr(importers_pkg, "inventory_infra")  # noqa: B009 -- dynamic alias
    assert (
        getattr(infra_mod, "InventoryImporter") is InventoryImporter  # noqa: B009 -- dynamic alias
    )
    inv_mod = getattr(importers_pkg, "_inventory")  # noqa: B009 -- dynamic alias
    assert (
        getattr(inv_mod, "InventorySourceImporter")  # noqa: B009 -- dynamic alias
        is InventorySourceImporter
    )
    dotted = importlib.import_module("aap_migration.migration.importers.templates")
    assert dotted is templates_alias


def test_factory_ignores_leaf_and_package_patches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mock guidance contract: patching a leaf, package, or shim attribute
    # has no effect once the table is built; patching the table entry does.
    sentinel = type("SentinelScheduleImporter", (ScheduleImporter,), {})
    monkeypatch.setattr(
        "aap_migration.migration.importers._schedules.ScheduleImporter",
        sentinel,
    )
    assert type(create_importer("schedules", None, None, None)) is ScheduleImporter

    monkeypatch.setattr(importers_pkg, "ScheduleImporter", sentinel)
    assert type(create_importer("schedules", None, None, None)) is ScheduleImporter

    specs = get_resource_specs()
    original = specs["schedules"]
    specs["schedules"] = ResourceSpec(
        importer_cls=sentinel, order=150, org_scoped=False, org_required=False
    )
    try:
        assert type(create_importer("schedules", None, None, None)) is sentinel
    finally:
        specs["schedules"] = original


def test_ordered_types_and_granular_steps() -> None:
    ordered = get_ordered_resource_types()
    assert ordered == EXPECTED_ORDER
    assert get_importer_class("schedules") is ScheduleImporter
    with pytest.raises(KeyError):
        get_importer_class("bogus-type")
    # The derived step list is the canonical-order subset for the menu,
    # pinned against a hardcoded snapshot independent of both live sources.
    assert DEFAULT_GRANULAR_STEPS == EXPECTED_GRANULAR_STEPS
    assert set(DEFAULT_GRANULAR_STEPS) <= set(ordered)
    assert DEFAULT_GRANULAR_STEPS == sorted(DEFAULT_GRANULAR_STEPS, key=ordered.index)
    assert "schedules" in DEFAULT_GRANULAR_STEPS


def test_micro_phases_match_granular_steps() -> None:
    from typing import Any, cast

    from aap_migration.cli.granular_import import MICRO_PHASES

    phases = cast(list[dict[str, Any]], MICRO_PHASES)
    menu_types = [str(phase["resource_type"]) for phase in phases if not phase.get("manual")]
    assert menu_types == EXPECTED_GRANULAR_STEPS
    assert DEFAULT_GRANULAR_STEPS == EXPECTED_GRANULAR_STEPS


def test_dependency_closure_matches_registry() -> None:
    # Regression: the CLI closure kept a stale 19-key copy of the 25-key
    # registry and silently under-imported filtered requests. It now derives
    # from the canonical table, so every factory type resolves identically.
    from aap_migration.cli.commands.export_import import get_importer_dependencies

    for resource_type in sorted(EXPECTED_TYPES.keys()):
        assert get_importer_dependencies(resource_type) == dict(
            get_importer_class(resource_type).DEPENDENCIES
        ), resource_type
    assert get_importer_dependencies("bogus-type") == {}


def test_dependency_content_snapshot_independent_of_registry() -> None:
    # Content snapshot pinned independently of the live table: a leaf typo
    # passes the registry-vs-itself parity above on both sides but fails
    # here, which is the actual regression guard.
    from aap_migration.cli.commands.export_import import get_importer_dependencies

    assert set(EXPECTED_DEPS.keys()) == set(EXPECTED_TYPES.keys())
    for resource_type in sorted(EXPECTED_DEPS.keys()):
        assert get_importer_dependencies(resource_type) == EXPECTED_DEPS[resource_type], (
            resource_type
        )


def test_dependency_closure_pulls_schedule_ujt_parents() -> None:
    # Schedules resolve unified_job_template manually per record, so the
    # closure must pull candidate parent types explicitly. Without this, a
    # schedule-only filtered import silently drops template parents.
    # Exact-tuple pin: subset-only checks pass on addition and silently
    # over-import, so widening and narrowing must both fail loudly.
    from aap_migration.cli.commands.export_import import (
        SCHEDULE_UJT_PARENT_TYPES,
        build_dependency_closure,
    )
    from aap_migration.validate.common import SCHEDULE_PARENT_TYPES

    assert SCHEDULE_UJT_PARENT_TYPES == EXPECTED_SCHEDULE_UJT_PARENTS
    assert SCHEDULE_PARENT_TYPES == EXPECTED_SCHEDULE_UJT_PARENTS
    assert SCHEDULE_UJT_PARENT_TYPES == SCHEDULE_PARENT_TYPES

    available = list(EXPECTED_ORDER)
    closure = build_dependency_closure(["schedules"], available)
    for parent in EXPECTED_SCHEDULE_UJT_PARENTS:
        assert parent in closure, parent
    assert closure.index("job_templates") < closure.index("schedules")
    assert closure.index("projects") < closure.index("schedules")


def test_organization_scope_parity() -> None:
    from aap_migration.resources import (
        ORGANIZATION_SCOPED_RESOURCES as resources_scoped,
    )

    assert importers_pkg.ORGANIZATION_SCOPED_RESOURCES == resources_scoped
    assert importers_pkg.ORGANIZATION_REQUIRED_RESOURCES == {
        "teams",
        "projects",
        "inventories",
        "notification_templates",
    }


def test_settings_mixin_wiring() -> None:
    from aap_migration.migration.importers._settings_gateway import (
        SettingsGatewayMixin,
    )
    from aap_migration.migration.importers.settings import SettingsImporter

    assert issubclass(SettingsImporter, SettingsGatewayMixin)
    assert hasattr(SettingsImporter, "_migrate_all_authentication_to_gateway")


@pytest.mark.anyio
async def test_get_dependencies_hook_preserved() -> None:
    # The base commit exposed _get_dependencies as an override point consulted
    # by _resolve_dependencies/_enrich_api_error_message; the split must keep
    # consulting it so downstream overrides are not silently ignored.
    importer = ScheduleImporter(MagicMock(), MagicMock(), MagicMock())
    assert importer._get_dependencies("schedules") == ScheduleImporter.DEPENDENCIES

    class CustomDeps(ScheduleImporter):
        def _get_dependencies(self, resource_type: str) -> dict[str, str]:
            return {"organization": "organizations"}

    state = MagicMock()
    state.get_mapped_id.return_value = 7
    custom = CustomDeps(MagicMock(), state, MagicMock())
    resolved = await custom._resolve_dependencies("schedules", {"organization": 3, "name": "s"})
    assert resolved["organization"] == 7


@pytest.mark.anyio
async def test_parallel_import_preserves_caller_dicts() -> None:
    # Move-guard for the split's one systematic behavior delta: the base
    # commit popped _source_id (mutating caller dicts) while the split reads
    # via .get and strips on a copy. Payloads must be identical (no
    # _source_id leaked to the API) and caller dicts must survive intact for
    # retry/resume passes.
    state = MagicMock()
    state.is_migrated.return_value = False
    importer = ScheduleImporter(MagicMock(), state, MagicMock())
    seen_payloads: list[dict] = []

    async def fake_import_resource(resource_type, source_id, data, resolve_dependencies=True):  # type: ignore[no-untyped-def]
        seen_payloads.append(dict(data))
        return {"id": source_id}

    importer.import_resource = fake_import_resource
    records = [
        {"_source_id": 5, "name": "a", "unified_job_template": 9},
        {"_source_id": 6, "name": "b"},
    ]
    snapshot = copy.deepcopy(records)
    await importer._import_parallel("schedules", records, concurrency=5)
    assert records == snapshot
    assert len(seen_payloads) == 2
    for payload in seen_payloads:
        assert "_source_id" not in payload


@pytest.mark.anyio
async def test_parallel_import_empty_and_progress() -> None:
    state = MagicMock()
    state.is_migrated.return_value = False
    importer = ScheduleImporter(MagicMock(), state, MagicMock())

    async def fake_import_resource(resource_type, source_id, data, resolve_dependencies=True):  # type: ignore[no-untyped-def]
        return {"id": source_id}

    importer.import_resource = fake_import_resource
    assert await importer._import_parallel("schedules", []) == []

    calls: list[tuple[int, int, int]] = []
    records = [{"_source_id": 1, "name": "a"}, {"_source_id": 2, "name": "b"}]
    await importer._import_parallel(
        "schedules", records, progress_callback=lambda s, f, sk: calls.append((s, f, sk))
    )
    assert calls and calls[-1][0] == 2


@pytest.mark.anyio
async def test_parallel_import_exception_marks_failed() -> None:
    state = MagicMock()
    state.is_migrated.return_value = False
    importer = ScheduleImporter(MagicMock(), state, MagicMock())

    async def boom(resource_type, source_id, data, resolve_dependencies=True):  # type: ignore[no-untyped-def]
        raise RuntimeError("api down")

    importer.import_resource = boom
    results = await importer._import_parallel(
        "schedules", [{"_source_id": 9, "name": "x"}], concurrency=2
    )
    assert results == []
    assert state.mark_failed.called
    assert importer.import_errors and importer.import_errors[0]["source_id"] == 9


@pytest.mark.anyio
async def test_parallel_import_already_migrated_skip() -> None:
    state = MagicMock()
    state.is_migrated.return_value = True
    importer = ScheduleImporter(MagicMock(), state, MagicMock())

    async def fake_import_resource(resource_type, source_id, data, resolve_dependencies=True):  # type: ignore[no-untyped-def]
        return None

    importer.import_resource = fake_import_resource
    results = await importer._import_parallel(
        "schedules", [{"_source_id": 3, "name": "s"}], concurrency=2
    )
    assert results == []
    assert not state.mark_failed.called


@pytest.mark.anyio
async def test_parallel_import_rejects_zero_concurrency() -> None:
    state = MagicMock()
    importer = ScheduleImporter(MagicMock(), state, MagicMock())
    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        await importer._import_parallel(
            "schedules", [{"_source_id": 1, "name": "a"}], concurrency=0
        )


@pytest.mark.anyio
async def test_parallel_import_malformed_row_marks_failed() -> None:
    state = MagicMock()
    state.is_migrated.return_value = False
    importer = ScheduleImporter(MagicMock(), state, MagicMock())
    results = await importer._import_parallel(
        "schedules",
        cast("list[dict[str, Any]]", [None]),
        concurrency=2,
    )
    assert results == []
    assert state.mark_failed.called


@pytest.mark.anyio
async def test_second_leaf_resolve_override_parity() -> None:
    # A second leaf beyond ScheduleImporter must consult the hook too, so a
    # split divergence in another override is caught.
    from aap_migration.migration.importers._job_templates import JobTemplateImporter

    state = MagicMock()
    state.get_mapped_id.return_value = 11
    importer = JobTemplateImporter(MagicMock(), state, MagicMock())
    resolved = await importer._resolve_dependencies(
        "job_templates", {"organization": 4, "name": "jt"}
    )
    assert resolved["organization"] == 11


def test_enriched_api_error_strings_pinned() -> None:
    # Golden test for the session-lookup fix: enriched strings must carry
    # source names when the DB lookup hits and fall back to ID-only text
    # when it misses, so future regressions are loud.
    from aap_migration.client.exceptions import APIError

    state = MagicMock()
    importer = ScheduleImporter(MagicMock(), state, MagicMock())
    error = APIError("bad", response={"inventory": ["Invalid pk 7"]})

    def _named_lookup(resource_type: str, source_id: int) -> str | None:
        return "Main"

    def _missing_lookup(resource_type: str, source_id: int) -> str | None:
        return None

    # setattr (not attribute assignment) so no method-assign ignore is needed.
    setattr(importer, "_get_dependency_name", _named_lookup)  # noqa: B010 -- dynamic patch
    enriched = importer._enrich_api_error_message(error, "schedules", {"inventory": 7, "name": "s"})
    assert "Main" in enriched and "7" in enriched

    setattr(importer, "_get_dependency_name", _missing_lookup)  # noqa: B010 -- dynamic patch
    fallback = importer._enrich_api_error_message(error, "schedules", {"inventory": 7, "name": "s"})
    assert "7" in fallback


def test_importer_file_size_guard() -> None:
    # The split decomposed an 8k file; base and execution sit within ~12
    # lines of the 1k guard. Fail loudly on breach so the next domain
    # addition forces a split instead of silent growth; tracked follow-up
    # is to extract validation, duplicate-detection, and the parallel
    # runner from base.
    repo_root = Path(__file__).resolve().parents[2]
    base_lines = len(
        (repo_root / "src/aap_migration/migration/importers/base.py").read_text().splitlines()
    )
    execution_lines = len(
        (repo_root / "src/aap_migration/migration/importers/execution.py").read_text().splitlines()
    )
    assert base_lines < 1000, f"base.py breached 1k: {base_lines}"
    assert execution_lines < 1000, f"execution.py breached 1k: {execution_lines}"


def test_leaf_loggers_are_module_attributed() -> None:
    # Every leaf module must log under its own channel, not base's: per-module
    # filtering broke when leaves shared base.logger.
    import aap_migration.migration.importers.base as base_module

    for dotted in sorted(set(CLASS_MODULES.values())):
        module = importlib.import_module(dotted)
        assert hasattr(module, "logger"), dotted
        assert module.logger is not base_module.logger, dotted


def test_dependency_closure_propagates_registry_drift_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fail-closed: the registry drift gate (AssertionError) and leaf import
    # failures (ImportError) must abort via the CLI closure, never degrade to
    # empty-deps silent under-import (review #1 on PR 139).
    from aap_migration.cli.commands import export_import as ei_module
    from aap_migration.migration.importers import _registry as registry

    def _boom_assertion(_resource_type: str):  # type: ignore[no-untyped-def]
        raise AssertionError("registry drift: flags-only=['bogus']")

    def _boom_import(_resource_type: str):  # type: ignore[no-untyped-def]
        raise ImportError("leaf import failed")

    monkeypatch.setattr(registry, "get_importer_class", _boom_assertion)
    with pytest.raises(AssertionError):
        ei_module.get_importer_dependencies("schedules")

    monkeypatch.setattr(registry, "get_importer_class", _boom_import)
    with pytest.raises(ImportError):
        ei_module.get_importer_dependencies("schedules")

    # Unknown types still degrade to {} (KeyError path preserved).
    monkeypatch.setattr(
        registry,
        "get_importer_class",
        lambda _rt: (_ for _ in ()).throw(KeyError(_rt)),
    )
    assert ei_module.get_importer_dependencies("bogus-type") == {}


@pytest.mark.parametrize("error", [RuntimeError, AttributeError, TypeError, ValueError])
def test_dependency_closure_fail_closed_for_non_import_errors(
    monkeypatch: pytest.MonkeyPatch, error: type[BaseException]
) -> None:
    # Any non-KeyError failure (broken leaf, poisoned descriptor, malformed
    # shape) must abort, not degrade to {} and silently prune the closure.
    from aap_migration.cli.commands import export_import as ei_module
    from aap_migration.migration.importers import _registry as registry

    def _boom(_resource_type: str):  # type: ignore[no-untyped-def]
        raise error("leaf boom")

    monkeypatch.setattr(registry, "get_importer_class", _boom)
    with pytest.raises(error):
        ei_module.get_importer_dependencies("schedules")


def test_build_specs_flags_vs_classes_skew_aborts() -> None:
    # Fire the real drift gate instead of mocking the raise: a flags-only
    # entry with no class must abort on first factory use.
    registry_module._RESOURCE_FLAGS["bogus-type"] = registry_module.ResourceFlags(999, False, False)
    registry_module.RESOURCE_SPECS.clear()
    registry_module._specs_built = False
    try:
        with pytest.raises(AssertionError, match="registry drift"):
            registry_module.get_resource_specs()
    finally:
        del registry_module._RESOURCE_FLAGS["bogus-type"]
        registry_module.RESOURCE_SPECS.clear()
        registry_module._specs_built = False
        reset_registry_for_tests()


def test_build_specs_unknown_granular_step_aborts() -> None:
    original_steps = registry_module._GRANULAR_STEP_NAMES
    registry_module._GRANULAR_STEP_NAMES = frozenset({"bogus-step"})
    registry_module.RESOURCE_SPECS.clear()
    registry_module._specs_built = False
    try:
        with pytest.raises(AssertionError, match="granular steps unknown"):
            registry_module.get_resource_specs()
    finally:
        registry_module._GRANULAR_STEP_NAMES = original_steps
        registry_module.RESOURCE_SPECS.clear()
        registry_module._specs_built = False
        reset_registry_for_tests()


def test_get_importer_dependencies_malformed_shape_raises_type_error() -> None:
    # Fail-closed: a leaf whose DEPENDENCIES is not a dict must abort via
    # TypeError, never degrade to {} and silently prune the closure.
    # Patches the live spec-table entry (the documented double point), not
    # get_importer_class, so export_import.py:149-153 execute for real.
    from aap_migration.cli.commands.export_import import get_importer_dependencies

    class MalformedDeps(ScheduleImporter):
        DEPENDENCIES = ["not", "a", "dict"]

    specs = get_resource_specs()
    original = specs["schedules"]
    specs["schedules"] = ResourceSpec(
        importer_cls=MalformedDeps, order=150, org_scoped=False, org_required=False
    )
    try:
        with pytest.raises(TypeError, match="is not a dict"):
            get_importer_dependencies("schedules")
    finally:
        specs["schedules"] = original


def test_get_importer_dependencies_missing_mapping_raises_attribute_error() -> None:
    # Fail-closed: a leaf class with no usable DEPENDENCIES mapping (deleted
    # or broken descriptor) must propagate AttributeError, never degrade to
    # {} and silently prune the closure.
    from aap_migration.cli.commands.export_import import get_importer_dependencies

    class NoDepsMapping:
        pass

    specs = get_resource_specs()
    original = specs["schedules"]
    specs["schedules"] = ResourceSpec(
        importer_cls=NoDepsMapping,
        order=150,
        org_scoped=False,
        org_required=False,
    )
    try:
        with pytest.raises(AttributeError):
            get_importer_dependencies("schedules")
    finally:
        specs["schedules"] = original


def test_dependency_closure_non_schedule_request_pulls_no_ujt_parents() -> None:
    # False-side of the schedules-only UJT guard (export_import.py): a
    # non-schedule filtered request must not pull unified-job-template
    # parents. Pinned against the hardcoded tuple so guard widening fails
    # here instead of passing on a live-vs-live comparison.
    from aap_migration.cli.commands.export_import import build_dependency_closure

    available = list(EXPECTED_ORDER)
    closure = build_dependency_closure(["inventories"], available)
    assert "inventories" in closure
    for parent in EXPECTED_SCHEDULE_UJT_PARENTS:
        assert parent not in closure, parent


def test_dependency_closure_skips_export_absent_ujt_parents_and_grandparents() -> None:
    # Schedule-only closure must intersect UJT parents with the export and
    # must not traverse deps of unavailable types. With no UJT parent
    # exported, grandparents (e.g. organizations via an absent projects
    # parent) must not be pulled: per-record UJT resolution keeps the stale
    # source ID and fails per row (API 400), so over-import plus per-row
    # failure is the failure mode this guards.
    from aap_migration.cli.commands.export_import import build_dependency_closure

    available = ["schedules", "organizations"]
    closure = build_dependency_closure(["schedules"], available)
    assert "schedules" in closure
    for parent in EXPECTED_SCHEDULE_UJT_PARENTS:
        assert parent not in closure, parent
    # Grandparent via an absent intermediate must not leak in either.
    assert "organizations" not in closure


def test_dependency_closure_partial_ujt_parents_pull_only_available() -> None:
    # Only available UJT parents are pulled; absent parents are surfaced
    # (warning) rather than traversed.
    from aap_migration.cli.commands.export_import import build_dependency_closure

    available = ["schedules", "job_templates", "organizations"]
    closure = build_dependency_closure(["schedules"], available)
    assert "schedules" in closure
    assert "job_templates" in closure
    assert "organizations" in closure
    assert "projects" not in closure
    assert "workflow_job_templates" not in closure
    assert "inventory_sources" not in closure


def test_get_importer_dependencies_unknown_value_raises_value_error() -> None:
    # Fail-closed on poisoned values: a DEPENDENCIES entry naming an unknown
    # resource (leaf typo or live-table mutation) must raise ValueError, never
    # degrade to {} and silently prune the closure.
    from aap_migration.cli.commands.export_import import get_importer_dependencies

    class PoisonedDeps(ScheduleImporter):
        DEPENDENCIES = {"organization": "organisations_typo"}

    specs = get_resource_specs()
    original = specs["schedules"]
    specs["schedules"] = ResourceSpec(
        importer_cls=PoisonedDeps, order=150, org_scoped=False, org_required=False
    )
    try:
        with pytest.raises(ValueError, match="unknown resource type"):
            get_importer_dependencies("schedules")
    finally:
        specs["schedules"] = original


def test_get_dependency_name_db_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Error path: a DB outage during API-error enrichment must fall back to
    # ID-only text (None), never mask the original import failure.
    import aap_migration.migration.database as database_module

    def _boom(_database_url: str | None = None) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(database_module, "get_session", _boom)
    importer = ScheduleImporter(MagicMock(), MagicMock(), MagicMock())
    assert importer._get_dependency_name("inventories", 7) is None


def test_get_dependency_name_missing_row_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Missing-row path: an unknown (resource_type, source_id) pair returns
    # None so the caller falls back to ID-only text.
    import aap_migration.migration.database as database_module

    queries: list[Any] = []

    class _EmptyQuery:
        def filter_by(self, **kwargs: Any) -> "_EmptyQuery":
            return self

        def first(self) -> Any:
            return None

    class _EmptySession:
        def query(self, _model: Any) -> "_EmptyQuery":
            queries.append(_model)
            return _EmptyQuery()

        def __enter__(self) -> "_EmptySession":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    def _fake_get_session(_database_url: str | None = None) -> Any:
        return _EmptySession()

    monkeypatch.setattr(database_module, "get_session", _fake_get_session)
    importer = ScheduleImporter(MagicMock(), MagicMock(), MagicMock())
    assert importer._get_dependency_name("inventories", 7) is None
    assert len(queries) == 1


def test_get_dependency_name_caches_successful_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cache path: repeated invalid-PK errors for the same dependency must
    # not fan out to N read transactions.
    import aap_migration.migration.database as database_module

    queries: list[Any] = []
    row = MagicMock()
    row.source_name = "Main"

    class _RowQuery:
        def filter_by(self, **kwargs: Any) -> "_RowQuery":
            return self

        def first(self) -> Any:
            return row

    class _RowSession:
        def query(self, _model: Any) -> "_RowQuery":
            queries.append(_model)
            return _RowQuery()

        def __enter__(self) -> "_RowSession":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    def _fake_get_session(_database_url: str | None = None) -> Any:
        return _RowSession()

    monkeypatch.setattr(database_module, "get_session", _fake_get_session)
    importer = ScheduleImporter(MagicMock(), MagicMock(), MagicMock())
    assert importer._get_dependency_name("inventories", 7) == "Main"
    assert importer._get_dependency_name("inventories", 7) == "Main"
    assert len(queries) == 1
