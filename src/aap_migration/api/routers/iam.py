"""IAM endpoints (mirrors ``iam audit|migrate|benchmark|report``)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from aap_migration.api import services
from aap_migration.api.jobs import TERMINAL_STATUSES
from aap_migration.api.routers._common import submit_chained
from aap_migration.api.schemas import (
    IamAuditRequest,
    IamBenchmarkRequest,
    IamMigrateRequest,
    IamReportRequest,
    JobCreated,
)

router = APIRouter(tags=["iam"])


@router.post("/iam/audit", response_model=JobCreated, status_code=202)
def iam_audit(body: IamAuditRequest, request: Request) -> JobCreated:
    """Read-only IAM scan of the source AAP (no target required).

    Like migrate-resume/retry, chains onto ``failed`` and ``cancelled`` jobs
    so ``resume=true`` with a previous job's ``job_id`` continues from its
    checkpoint instead of 409ing.
    """
    return submit_chained(
        "iam-audit",
        body,
        services.run_iam_audit,
        need="source",
        allow_statuses=TERMINAL_STATUSES,
        root_path=request.scope.get("root_path", ""),
    )


@router.post("/iam/migrate", response_model=JobCreated, status_code=202)
def iam_migrate(body: IamMigrateRequest, request: Request) -> JobCreated:
    """Migrate IAM permissions to the target AAP (supports two-phase LDAP flow)."""
    # skip_user_roles/users_only exclusion lives in IamMigrateRequest (single home).
    return submit_chained(
        "iam-migrate",
        body,
        services.run_iam_migrate,
        allow_statuses=TERMINAL_STATUSES,
        root_path=request.scope.get("root_path", ""),
    )


@router.post("/iam/benchmark", response_model=JobCreated, status_code=202)
def iam_benchmark(body: IamBenchmarkRequest, request: Request) -> JobCreated:
    """Benchmark API latency/concurrency to size ``workers``."""
    return submit_chained(
        "iam-benchmark",
        body,
        services.run_iam_benchmark,
        need="source",
        root_path=request.scope.get("root_path", ""),
    )


@router.post("/iam/report", response_model=JobCreated, status_code=202)
def iam_report(body: IamReportRequest, request: Request) -> JobCreated:
    """Regenerate the IAM HTML report from a previous JSON export."""
    # IamReportRequest requires json_path or job_id (schema-level). Unknown
    # job ids fail fast with 404 inside submit_chained; pending/failed jobs
    # fail at execution with a clear error (see services.run_iam_report).
    return submit_chained(
        "iam-report",
        body,
        services.run_iam_report,
        need="none",
        root_path=request.scope.get("root_path", ""),
    )


@router.get("/iam/checkpoint")
def iam_checkpoint() -> dict:
    """Describe the IAM checkpoint mechanism (mirrors ``--resume`` support)."""
    return {
        "description": (
            "IAM audit/migrate jobs support resume via checkpoint files stored "
            "in the job directory (iam_reports/iam_checkpoint.json). "
            "Re-submit with resume=true to skip completed work units."
        ),
        "checkpoint_file": "iam_reports/iam_checkpoint.json",
    }
