"""Dependency graph operations for migration ordering."""

from __future__ import annotations


def topological_sort(graph: dict[str, list[str]]) -> list[str]:
    """Topologically sort organizations based on dependencies.

    Uses Kahn's algorithm to determine migration order.
    Organizations with no dependencies come first.

    Args:
        graph: Dictionary mapping org_name -> list of dependency org_names
               Example: {"Default": ["Engineering", "IT Ops"], "Engineering": []}

    Returns:
        List of organization names in migration order (dependencies first)

    Raises:
        ValueError: If circular dependencies detected

    Example:
        >>> graph = {
        ...     "Default": ["Engineering"],
        ...     "Engineering": [],
        ...     "DevOps": ["Engineering", "Default"]
        ... }
        >>> topological_sort(graph)
        ['Engineering', 'Default', 'DevOps']
    """
    # Build in-degree map (how many dependencies each org has)
    in_degree = dict.fromkeys(graph, 0)

    for _org, deps in graph.items():
        for dep in deps:
            if dep not in in_degree:
                in_degree[dep] = 0

    for org, deps in graph.items():
        in_degree[org] = len(deps)

    queue = [org for org, degree in in_degree.items() if degree == 0]
    result = []

    while queue:
        queue.sort()
        org = queue.pop(0)
        result.append(org)

        for other_org, deps in graph.items():
            if org in deps:
                in_degree[other_org] -= 1
                if in_degree[other_org] == 0:
                    queue.append(other_org)

    if len(result) != len(in_degree):
        remaining = set(in_degree.keys()) - set(result)
        raise ValueError(f"Circular dependencies detected among organizations: {remaining}")

    return result


def _partial_topological_sort(graph: dict[str, list[str]]) -> tuple[list[str], set[str]]:
    """Sort as many orgs as possible; return (sorted, cycle_members)."""
    in_degree: dict[str, int] = {}
    for org in graph:
        in_degree.setdefault(org, 0)
        for dep in graph[org]:
            in_degree.setdefault(dep, 0)

    for org, deps in graph.items():
        in_degree[org] = len([d for d in deps if d in in_degree])

    queue = sorted(org for org, deg in in_degree.items() if deg == 0)
    result: list[str] = []

    while queue:
        org = queue.pop(0)
        result.append(org)
        for other_org, deps in graph.items():
            if org in deps:
                in_degree[other_org] -= 1
                if in_degree[other_org] == 0:
                    queue.append(other_org)
                    queue.sort()

    cycle_members = set(in_degree.keys()) - set(result)
    return result, cycle_members


def detect_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Detect circular dependencies in the graph.

    Returns each cycle cluster as a sorted list of organization names.
    """
    _, cycle_members = _partial_topological_sort(graph)
    return [sorted(cycle_members)] if cycle_members else []


def group_into_phases(
    graph: dict[str, list[str]], migration_order: list[str]
) -> list[dict[str, int | str | list[str]]]:
    """Group organizations into migration phases.

    Organizations in the same phase can be migrated in parallel
    (they don't depend on each other).

    Args:
        graph: Dependency graph
        migration_order: Topologically sorted order

    Returns:
        List of phases, each containing orgs that can migrate in parallel

    Example:
        >>> graph = {
        ...     "A": [], "B": [], "C": ["A"], "D": ["A", "B"]
        ... }
        >>> order = ["A", "B", "C", "D"]
        >>> group_into_phases(graph, order)
        [
            {"phase": 1, "orgs": ["A", "B"], "description": "..."},
            {"phase": 2, "orgs": ["C", "D"], "description": "..."}
        ]
    """
    phases: list[dict[str, int | str | list[str]]] = []
    processed: set[str] = set()

    while len(processed) < len(migration_order):
        phase_orgs = []
        for org in migration_order:
            if org in processed:
                continue
            deps = graph.get(org, [])
            if all(dep in processed for dep in deps):
                phase_orgs.append(org)

        if not phase_orgs:
            remaining = set(migration_order) - processed
            raise ValueError(f"Cannot group remaining orgs: {remaining}")

        if len(phases) == 0:
            description = "Independent organizations (no dependencies)"
        else:
            description = f"Organizations dependent on Phase {len(phases)} migrations"

        phases.append(
            {
                "phase": len(phases) + 1,
                "orgs": sorted(phase_orgs),
                "description": description,
            }
        )

        processed.update(phase_orgs)

    return phases


def group_into_phases_with_cycles(
    graph: dict[str, list[str]],
) -> tuple[list[str], list[dict[str, int | str | list[str]]]]:
    """Build migration order and phases, handling circular dependencies gracefully.

    Independent orgs go first, then orgs whose deps are satisfied, then cycle
    members are placed together in their own phase so users can manually reorder.

    Returns:
        (migration_order, phases)
    """
    sorted_orgs, cycle_members = _partial_topological_sort(graph)

    if not cycle_members:
        return sorted_orgs, group_into_phases(graph, sorted_orgs)

    # Build phases from the non-cycle orgs first
    phases: list[dict[str, int | str | list[str]]] = []
    processed: set[str] = set()

    while len(processed) < len(sorted_orgs):
        phase_orgs = []
        for org in sorted_orgs:
            if org in processed:
                continue
            deps = graph.get(org, [])
            if all(d in processed or d in cycle_members for d in deps):
                phase_orgs.append(org)

        if not phase_orgs:
            break

        if len(phases) == 0:
            description = "Independent organizations (no dependencies)"
        else:
            description = f"Organizations dependent on Phase {len(phases)} migrations"

        phases.append(
            {
                "phase": len(phases) + 1,
                "orgs": sorted(phase_orgs),
                "description": description,
            }
        )
        processed.update(phase_orgs)

    # Add cycle members as their own phase
    phases.append(
        {
            "phase": len(phases) + 1,
            "orgs": sorted(cycle_members),
            "description": "Organizations with circular dependencies (review and reorder manually)",
        }
    )
    cycle_processed = set(cycle_members)

    # Any remaining orgs that depend on cycle members go in subsequent phases
    all_orgs = set(graph.keys())
    remaining = all_orgs - processed - cycle_processed
    remaining_order = sorted(remaining)
    processed.update(cycle_processed)

    while remaining_order:
        phase_orgs = []
        for org in remaining_order:
            if org in processed:
                continue
            deps = graph.get(org, [])
            if all(d in processed for d in deps):
                phase_orgs.append(org)

        if not phase_orgs:
            # Remaining orgs can't be resolved — dump them into final phase
            leftover = sorted(set(remaining_order) - processed)
            if leftover:
                phases.append(
                    {
                        "phase": len(phases) + 1,
                        "orgs": leftover,
                        "description": "Remaining organizations (unresolved dependencies)",
                    }
                )
            break

        phases.append(
            {
                "phase": len(phases) + 1,
                "orgs": sorted(phase_orgs),
                "description": f"Organizations dependent on Phase {len(phases)} migrations",
            }
        )
        processed.update(phase_orgs)
        remaining_order = [o for o in remaining_order if o not in processed]

    migration_order = sorted_orgs + sorted(cycle_members) + sorted(remaining)
    return migration_order, phases
