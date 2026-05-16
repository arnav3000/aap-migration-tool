"""Compatibility shim - re-exports from dependency_graph."""

from aap_migration.analysis.dependency_graph import (
    detect_cycles,
    group_into_phases,
    group_into_phases_with_cycles,
    topological_sort,
)

__all__ = [
    "topological_sort",
    "group_into_phases",
    "group_into_phases_with_cycles",
    "detect_cycles",
]
