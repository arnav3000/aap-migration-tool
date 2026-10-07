"""Analysis endpoints (mirrors ``analyze-dependencies``)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from aap_migration.api import services
from aap_migration.api.routers._common import submit_chained
from aap_migration.api.schemas import AnalyzeDependenciesRequest, JobCreated, MigrationPlanOut

router = APIRouter(tags=["analysis"])

# Explicit public contract: new helpers in dependency_graph.py are NOT
# published until they are added here (no dir() introspection).
MIGRATION_PLAN_HELPERS = [
    "group_into_phases",
    "topological_sort",
]


@router.post("/analysis/dependencies", response_model=JobCreated, status_code=202)
def analyze_dependencies(body: AnalyzeDependenciesRequest, request: Request) -> JobCreated:
    """Analyze cross-organization dependencies for migration planning."""
    # Scope rules live in AnalyzeDependenciesRequest (single home).
    return submit_chained(
        "analyze-dependencies",
        body,
        services.run_analyze_dependencies,
        need="source",
        root_path=request.scope.get("root_path", ""),
    )


@router.get("/analysis/migration-plan", response_model=MigrationPlanOut)
def migration_plan() -> dict:
    """Pure dependency-graph helpers (topological order, phases, cycles)."""
    return {
        "description": (
            "Migration planning primitives from analysis/dependency_graph.py. "
            "Submit POST /analysis/dependencies for a full cross-org report."
        ),
        "helpers": list(MIGRATION_PLAN_HELPERS),
    }
