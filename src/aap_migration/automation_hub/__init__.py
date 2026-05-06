"""
Automation Hub Migration Module

This module provides functionality for migrating Automation Hub content
(Ansible Collections, Namespaces, Repositories) from AAP 2.4 to AAP 2.6.
"""

from aap_migration.automation_hub.client import GalaxyAPIClient
from aap_migration.automation_hub.exceptions import (
    AutomationHubError,
    GalaxyAPIError,
    NamespaceError,
    CollectionError,
    ArtifactError,
)
from aap_migration.automation_hub.models import (
    Namespace,
    Collection,
    CollectionVersion,
    Repository,
    RemoteRegistry,
    ExecutionEnvironment,
    ContainerRepository,
    ContainerRemoteRegistry,
)
from aap_migration.automation_hub.exporter import AutomationHubExporter
from aap_migration.automation_hub.transformer import AutomationHubTransformer
from aap_migration.automation_hub.importer import AutomationHubImporter

__all__ = [
    # Client
    "GalaxyAPIClient",
    # Migration Pipeline
    "AutomationHubExporter",
    "AutomationHubTransformer",
    "AutomationHubImporter",
    # Exceptions
    "AutomationHubError",
    "GalaxyAPIError",
    "NamespaceError",
    "CollectionError",
    "ArtifactError",
    # Models
    "Namespace",
    "Collection",
    "CollectionVersion",
    "Repository",
    "RemoteRegistry",
    "ExecutionEnvironment",
    "ContainerRepository",
    "ContainerRemoteRegistry",
]
