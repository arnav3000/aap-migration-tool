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
    in_degree = {org: 0 for org in graph}

    for org, deps in graph.items():
        for dep in deps:
            # Add dependency org if not in graph
            if dep not in in_degree:
                in_degree[dep] = 0

    # Count in-degrees
    for org, deps in graph.items():
        in_degree[org] = len(deps)

    # Find orgs with no dependencies
    queue = [org for org, degree in in_degree.items() if degree == 0]
    result = []

    while queue:
        # Sort queue for deterministic output (alphabetical for same level)
        queue.sort()

        # Take org with no remaining dependencies
        org = queue.pop(0)
        result.append(org)

        # Reduce in-degree for orgs that depend on this one
        for other_org, deps in graph.items():
            if org in deps:
                in_degree[other_org] -= 1
                if in_degree[other_org] == 0:
                    queue.append(other_org)

    # Check for circular dependencies
    if len(result) != len(in_degree):
        remaining = set(in_degree.keys()) - set(result)
        raise ValueError(
            f"Circular dependencies detected among organizations: {remaining}"
        )

    return result


def group_into_phases(
    graph: dict[str, list[str]],
    migration_order: list[str]
) -> list[dict[str, list[str]]]:
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
    phases = []
    processed = set()

    while len(processed) < len(migration_order):
        # Find orgs whose dependencies are all processed
        phase_orgs = []
        for org in migration_order:
            if org in processed:
                continue

            deps = graph.get(org, [])
            if all(dep in processed for dep in deps):
                phase_orgs.append(org)

        if not phase_orgs:
            # Should not happen if topological_sort worked
            remaining = set(migration_order) - processed
            raise ValueError(f"Cannot group remaining orgs: {remaining}")

        # Determine phase description
        if len(phases) == 0:
            description = "Independent organizations (no dependencies)"
        else:
            description = f"Organizations dependent on Phase {len(phases)} migrations"

        phases.append({
            "phase": len(phases) + 1,
            "orgs": sorted(phase_orgs),  # Sort for deterministic output
            "description": description
        })

        processed.update(phase_orgs)

    return phases
