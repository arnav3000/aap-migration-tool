"""Factory parity: create_importer must cover every base-commit resource type.

Regression test for review finding #1 (the domain split dropped the
``schedules`` entry, so schedule imports raised NotImplementedError).
"""

from aap_migration.migration.importers import ScheduleImporter, create_importer
from aap_migration.migration.importers.templates import JobTemplateImporter


def test_create_importer_covers_schedules() -> None:
    importer = create_importer("schedules", None, None, None)  # type: ignore[arg-type]
    assert isinstance(importer, ScheduleImporter)


def test_create_importer_covers_all_base_map_keys() -> None:
    expected = {
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
    for resource_type in sorted(expected):
        importer = create_importer(resource_type, None, None, None)  # type: ignore[arg-type]
        assert importer is not None, resource_type
    # Spot-check a neighboring entry so a future drop is localized.
    assert isinstance(
        create_importer("job_templates", None, None, None),  # type: ignore[arg-type]
        JobTemplateImporter,
    )
