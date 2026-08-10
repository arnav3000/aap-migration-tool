"""Importer registry and factory."""

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.config import PerformanceConfig
from aap_migration.migration.importers.applications import ApplicationImporter
from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.migration.importers.credential_input_sources import CredentialInputSourceImporter
from aap_migration.migration.importers.credential_types import CredentialTypeImporter
from aap_migration.migration.importers.credentials import CredentialImporter
from aap_migration.migration.importers.execution_environments import ExecutionEnvironmentImporter
from aap_migration.migration.importers.hosts import HostImporter, HostInventoryMembershipImporter
from aap_migration.migration.importers.instances import InstanceGroupImporter, InstanceImporter
from aap_migration.migration.importers.inventories import (
    InventoryGroupImporter,
    InventoryImporter,
    InventorySourceImporter,
)
from aap_migration.migration.importers.job_templates import JobTemplateImporter
from aap_migration.migration.importers.labels import LabelImporter
from aap_migration.migration.importers.notification_templates import NotificationTemplateImporter
from aap_migration.migration.importers.organizations import OrganizationImporter
from aap_migration.migration.importers.projects import ProjectImporter
from aap_migration.migration.importers.rbac import RBACImporter
from aap_migration.migration.importers.schedules import ScheduleImporter
from aap_migration.migration.importers.settings import SettingsImporter
from aap_migration.migration.importers.system_job_templates import SystemJobTemplateImporter
from aap_migration.migration.importers.users import TeamImporter, UserImporter
from aap_migration.migration.importers.workflows import WorkflowImporter
from aap_migration.migration.state import MigrationState

IMPORTER_REGISTRY: dict[str, type[ResourceImporter]] = {
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
    "job_templates": JobTemplateImporter,
    "workflow_job_templates": WorkflowImporter,
    "schedules": ScheduleImporter,
    "notification_templates": NotificationTemplateImporter,
    "rbac": RBACImporter,
    "system_job_templates": SystemJobTemplateImporter,
    "applications": ApplicationImporter,
    "settings": SettingsImporter,
}


def create_importer(
    resource_type: str,
    client: AAPTargetClient,
    state: MigrationState,
    performance_config: PerformanceConfig,
    resource_mappings: dict[str, dict[str, str]] | None = None,
    name_prefix: str = "",
) -> ResourceImporter:
    """Create appropriate importer for resource type."""
    importer_class = IMPORTER_REGISTRY.get(resource_type)
    if not importer_class:
        raise NotImplementedError(
            f"No importer implemented for resource type: {resource_type}. "
            f"Available importers: {', '.join(sorted(IMPORTER_REGISTRY.keys()))}"
        )

    return importer_class(
        client,
        state,
        performance_config,
        resource_mappings,
        name_prefix=name_prefix,
    )
