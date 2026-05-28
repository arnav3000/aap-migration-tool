"""Dependency graph operations for migration ordering.

The primary entry point is `compute_migration_plan(graph)`, which performs
SCC detection, condensation, topological ordering, and phase grouping in a
single pass. The older `topological_sort`, `group_into_phases`, and
`find_cycles` functions are retained as thin wrappers for backward
compatibility but each internally calls `compute_migration_plan`.

Cyclic dependencies are tolerated, not rejected: members of a cycle appear
together in the migration order, share the same phase, and are surfaced
explicitly in `MigrationPlan.cycles` for the caller to handle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class MigrationPlan:
    """Complete migration plan derived from a cross-org dependency graph.

    Attributes:
        order:  Flat migration sequence, dependencies first. Members of a
                cycle appear adjacent in the list.
        phases: Parallel-safe phase grouping. Each phase is a dict with
                keys: phase (int), orgs (list[str]), description (str),
                has_cycle (bool), cycles (list[list[str]]).
        cycles: Strongly connected components of size > 1. Each is a list
                of mutually dependent org names (alphabetically sorted).
        sccs:   All SCCs (including singletons), in topological order
                relative to the condensation DAG. Exposed for advanced
                callers; most code only needs `cycles`.
    """

    order: list[str] = field(default_factory=list)
    phases: list[dict[str, Any]] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    sccs: list[list[str]] = field(default_factory=list)


def find_strongly_connected_components(
    graph: dict[str, list[str]],
) -> list[list[str]]:
    """Find SCCs using an iterative implementation of Tarjan's algorithm.

    Iterative form avoids Python's recursion limit on deeply nested
    dependency chains. Behaviour is otherwise identical to the textbook
    recursive version.

    Args:
        graph: Mapping of org_name -> list of dependency org_names. Orgs
               that appear only as dependencies (not as keys) are still
               included as singleton SCCs.

    Returns:
        SCCs in reverse-topological order (sinks first). Each SCC's members
        are returned alphabetically sorted for determinism.
    """
    # Every node mentioned anywhere participates in the SCC search.
    all_nodes: set[str] = set(graph.keys())
    for deps in graph.values():
        all_nodes.update(deps)

    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    node_stack: list[str] = []
    sccs: list[list[str]] = []
    counter = 0

    # Work stack entries: (node, iterator-over-successors).
    # Acts as a manual call stack in place of recursion.
    work: list[tuple[str, Iterator[str]]] = []

    for start in sorted(all_nodes):
        if start in index:
            continue

        # Begin exploring `start`.
        index[start] = counter
        lowlink[start] = counter
        counter += 1
        node_stack.append(start)
        on_stack.add(start)
        work.append((start, iter(graph.get(start, []))))

        while work:
            node, succ_iter = work[-1]
            # `next(it, None)` distinguishes exhaustion from values, since
            # successors are non-None strings.
            succ = next(succ_iter, None)

            if succ is None:
                # All successors processed: emit SCC if `node` is a root.
                if lowlink[node] == index[node]:
                    component: list[str] = []
                    while True:
                        w = node_stack.pop()
                        on_stack.discard(w)
                        component.append(w)
                        if w == node:
                            break
                    sccs.append(sorted(component))

                # Pop and propagate lowlink up to the parent frame.
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])
            elif succ not in index:
                # Descend into an unvisited successor.
                index[succ] = counter
                lowlink[succ] = counter
                counter += 1
                node_stack.append(succ)
                on_stack.add(succ)
                work.append((succ, iter(graph.get(succ, []))))
            elif succ in on_stack:
                # Back-edge to an ancestor still being explored.
                lowlink[node] = min(lowlink[node], index[succ])
            # else: cross-edge to a closed SCC — ignored, as per Tarjan.

    return sccs


def compute_migration_plan(graph: dict[str, list[str]]) -> MigrationPlan:
    """Compute the full migration plan from a dependency graph.

    Single-pass replacement for separate calls to `topological_sort`,
    `group_into_phases`, and `find_cycles`. SCC detection is done once;
    everything else is derived from the condensation DAG.

    The condensation is guaranteed to be acyclic, so the Kahn's-algorithm
    pass over it cannot fail — this function never raises for cyclic input.

    Args:
        graph: Mapping of org_name -> list of dependency org_names.

    Returns:
        MigrationPlan with order, phases, cycles, and raw SCCs.
    """
    sccs = find_strongly_connected_components(graph)
    n = len(sccs)

    if n == 0:
        return MigrationPlan()

    # Map each org to the index of its SCC in `sccs`.
    node_to_scc: dict[str, int] = {
        node: i for i, scc in enumerate(sccs) for node in scc
    }

    # Build the condensation DAG. scc_deps[i] = SCC indices that SCC i
    # depends on; reverse_deps[i] = SCC indices that depend on SCC i.
    scc_deps: list[set[int]] = [set() for _ in range(n)]
    reverse_deps: list[set[int]] = [set() for _ in range(n)]
    for org, deps in graph.items():
        i = node_to_scc[org]
        for dep in deps:
            j = node_to_scc.get(dep)
            if j is not None and j != i:
                if j not in scc_deps[i]:
                    scc_deps[i].add(j)
                    reverse_deps[j].add(i)

    # Kahn's on the condensation, with level assignment piggybacked on.
    # Level = 1 for SCCs with no deps; otherwise 1 + max level of any dep.
    in_degree = [len(d) for d in scc_deps]
    levels: list[int] = [0] * n
    queue: list[int] = []
    for i in range(n):
        if in_degree[i] == 0:
            levels[i] = 1
            queue.append(i)

    sorted_sccs: list[int] = []
    while queue:
        # Deterministic tie-breaking: lexicographically smallest member.
        queue.sort(key=lambda idx: sccs[idx][0])
        i = queue.pop(0)
        sorted_sccs.append(i)

        for j in reverse_deps[i]:
            in_degree[j] -= 1
            # Take the longest path: level of `j` is one more than the
            # deepest of its deps.
            levels[j] = max(levels[j], levels[i] + 1)
            if in_degree[j] == 0:
                queue.append(j)

    # --- Derive outputs ---

    order: list[str] = []
    for i in sorted_sccs:
        order.extend(sccs[i])  # already alpha-sorted

    cycles: list[list[str]] = [sccs[i] for i in sorted_sccs if len(sccs[i]) > 1]

    # Phase grouping by level.
    by_level: dict[int, list[int]] = {}
    for i in sorted_sccs:
        by_level.setdefault(levels[i], []).append(i)

    phases: list[dict[str, Any]] = []
    for level in sorted(by_level):
        scc_indices = by_level[level]
        orgs: list[str] = []
        phase_cycles: list[list[str]] = []
        for i in scc_indices:
            orgs.extend(sccs[i])
            if len(sccs[i]) > 1:
                phase_cycles.append(sccs[i])

        if level == 1:
            description = "Independent organizations (no dependencies)"
        else:
            description = (
                f"Organizations dependent on Phase {level - 1} migrations"
            )
        if phase_cycles:
            description += " [contains cyclic dependencies]"

        phases.append({
            "phase": level,
            "orgs": sorted(orgs),
            "description": description,
            "has_cycle": bool(phase_cycles),
            "cycles": phase_cycles,
        })

    return MigrationPlan(order=order, phases=phases, cycles=cycles, sccs=sccs)


# ---------------------------------------------------------------------------
# Backward-compatible wrappers. New code should call compute_migration_plan
# directly to avoid recomputing the SCC structure for each piece of output.
# ---------------------------------------------------------------------------


def topological_sort(graph: dict[str, list[str]]) -> list[str]:
    """Topologically sort organizations based on dependencies.

    Tolerant of cycles: members of a cycle appear adjacent in the output.
    For richer output (phases, cycle membership), use
    `compute_migration_plan` instead.

    Args:
        graph: Mapping of org_name -> list of dependency org_names.

    Returns:
        Migration order (dependencies first). Never raises for cyclic input.
    """
    return compute_migration_plan(graph).order


def group_into_phases(
    graph: dict[str, list[str]],
    migration_order: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Group organizations into parallel-safe migration phases.

    The `migration_order` argument is accepted for backward compatibility
    but is no longer used; phases are derived directly from the condensation.

    Args:
        graph:           Mapping of org_name -> list of dependency org_names.
        migration_order: Ignored. Present only for API stability.

    Returns:
        List of phase dicts. See `MigrationPlan.phases` for the shape.
    """
    del migration_order  # explicitly unused
    return compute_migration_plan(graph).phases


def find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Return only the SCCs that represent actual cycles (size > 1).

    Args:
        graph: Mapping of org_name -> list of dependency org_names.

    Returns:
        List of cycles, each a list of org names (alphabetically sorted).
    """
    return compute_migration_plan(graph).cycles
