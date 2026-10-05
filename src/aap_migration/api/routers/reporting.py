"""Reporting endpoints.

Mirrors ``migration-report``, ``enhanced-report`` and
``analyze-project-failures``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from aap_migration.api import services
from aap_migration.api.routers._common import submit_chained
from aap_migration.api.schemas import (
    EnhancedReportRequest,
    JobCreated,
    MigrationReportRequest,
    ProjectFailuresRequest,
)

router = APIRouter(tags=["reporting"])


@router.post("/reports/migration", response_model=JobCreated, status_code=202)
def migration_report(body: MigrationReportRequest, request: Request) -> JobCreated:
    """Failure analysis report comparing exports vs imports."""
    return submit_chained(
        "migration-report",
        body,
        services.run_migration_report,
        root_path=request.scope.get("root_path", ""),
    )


@router.post("/reports/enhanced", response_model=JobCreated, status_code=202)
def enhanced_report(body: EnhancedReportRequest, request: Request) -> JobCreated:
    """Enriched org report (staleness, last job runs, emails, error keys)."""
    return submit_chained(
        "enhanced-report",
        body,
        services.run_enhanced_report,
        root_path=request.scope.get("root_path", ""),
    )


@router.post("/reports/project-failures", response_model=JobCreated, status_code=202)
def project_failures(body: ProjectFailuresRequest, request: Request) -> JobCreated:
    """Inspect failed project imports and emit manual fix steps."""
    return submit_chained(
        "project-failures",
        body,
        services.run_project_failures,
        root_path=request.scope.get("root_path", ""),
    )
