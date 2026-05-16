"""Analysis module for dependency detection and risk assessment."""

from aap_migration.analysis.dependency_analyzer import (
    CrossOrgDependencyAnalyzer,
    GlobalDependencyReport,
    OrgDependencyReport,
    ResourceDependency,
)

__all__ = [
    "CrossOrgDependencyAnalyzer",
    "GlobalDependencyReport",
    "OrgDependencyReport",
    "ResourceDependency",
]
