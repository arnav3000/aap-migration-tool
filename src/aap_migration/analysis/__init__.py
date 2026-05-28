"""Cross-organization dependency analysis for migration planning."""

from aap_migration.analysis.dependency_analyzer import (
    CrossOrgDependencyAnalyzer,
    GlobalDependencyReport,
    OrgDependencyReport,
    ResourceDependency,
)
from aap_migration.analysis.dependency_graph import (
    MigrationPlan,
    compute_migration_plan,
    find_cycles,
    find_strongly_connected_components,
    group_into_phases,
    topological_sort,
)
from aap_migration.analysis.reports import (
    format_detailed_report,
    format_summary_report,
)

__all__ = [
    "CrossOrgDependencyAnalyzer",
    "GlobalDependencyReport",
    "MigrationPlan",
    "OrgDependencyReport",
    "ResourceDependency",
    "compute_migration_plan",
    "find_cycles",
    "find_strongly_connected_components",
    "format_detailed_report",
    "format_summary_report",
    "group_into_phases",
    "topological_sort",
]
