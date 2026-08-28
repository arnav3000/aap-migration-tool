"""IAM endpoints — API wrapper around the shared IAM engine.

CLI: ``src/aap_migration/cli/commands/iam.py`` (``IAMAnalyser`` + ``iam/report.py``)
API: this router (same ``iam/analyser.py:IAMAnalyser`` + ``iam/report.py``)

Endpoints:
  POST /api/iam/audit            -> start IAM audit job
  POST /api/iam/migrate          -> start IAM migration job
  POST /api/iam/benchmark        -> run benchmark (sync, returns text summary)
  GET  /api/iam/{job_id}         -> job status + result
  GET  /api/iam/{job_id}/export/json
  GET  /api/iam/{job_id}/export/html
  POST /api/iam/report           -> regenerate HTML from previous JSON (via job_id)
"""

from __future__ import annotations

import io
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, get_db_url, get_job_service
from aap_migration.api.schemas import (
    IAMAuditRequest,
    IAMBenchmarkRequest,
    IAMMigrateRequest,
    IAMReportRequest,
    JobStartResponse,
)
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.iam_service import (
    iam_audit_job_coro_factory,
    iam_migrate_job_coro_factory,
)
from aap_migration.api.services.job_service import JobStatus

router = APIRouter()


@router.post("/iam/audit", response_model=JobStartResponse)
async def iam_audit(body: IAMAuditRequest, db: Session = Depends(get_db)) -> JobStartResponse:
    """Read-only IAM scan — exports permission matrix (no target required)."""
    source = ConnectionService.get(db, body.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source connection not found")

    if body.scan_strategy not in ("resource", "principal"):
        raise HTTPException(status_code=400, detail="scan_strategy must be 'resource' or 'principal'")

    svc = get_job_service()
    coro_factory = iam_audit_job_coro_factory(
        source,
        verify_ssl=body.verify_ssl,
        timeout=body.timeout,
        workers=body.workers,
        scan_strategy=body.scan_strategy,
        resume=body.resume,
        checkpoint_dir=body.checkpoint_dir,
    )

    job_name = f"IAM audit {source.name} ({body.scan_strategy}, {body.workers}w)"
    job_id = svc.start_job(job_name, "iam-audit", coro_factory)
    return JobStartResponse(job_id=job_id)


@router.post("/iam/migrate", response_model=JobStartResponse)
async def iam_migrate(body: IAMMigrateRequest, db: Session = Depends(get_db)) -> JobStartResponse:
    """Migrate IAM permissions to target AAP.

    Supports ``dry_run``, ``skip_user_roles`` / ``users_only`` two-phase
    workflow, and ``scan_strategy`` — same flags as ``cli/commands/iam.py``.
    """
    source = ConnectionService.get(db, body.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source connection not found")

    target = ConnectionService.get(db, body.destination_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Destination connection not found")

    if body.skip_user_roles and body.users_only:
        raise HTTPException(status_code=400, detail="--skip-user-roles and --users-only are mutually exclusive")

    if body.scan_strategy not in ("resource", "principal"):
        raise HTTPException(status_code=400, detail="scan_strategy must be 'resource' or 'principal'")

    svc = get_job_service()
    db_url = get_db_url()
    try:
        bind_url = str(db.get_bind().url)
        if bind_url.startswith("sqlite") and db_url.startswith("postgresql://"):
            db_url = bind_url
    except Exception:  # nosec B110
        pass

    coro_factory = iam_migrate_job_coro_factory(
        source,
        target,
        state_db_path=body.state_db_path,
        db_url=db_url,
        verify_ssl=body.verify_ssl,
        timeout=body.timeout,
        workers=body.workers,
        scan_strategy=body.scan_strategy,
        dry_run=body.dry_run,
        skip_user_roles=body.skip_user_roles,
        users_only=body.users_only,
        resume=body.resume,
        checkpoint_dir=body.checkpoint_dir,
    )

    label = "dry-run" if body.dry_run else "migrate"
    if body.skip_user_roles:
        label += " teams-only"
    elif body.users_only:
        label += " users-only"
    job_name = f"IAM {label} {source.name} → {target.name}"

    job_id = svc.start_job(job_name, "iam-migrate", coro_factory)
    return JobStartResponse(job_id=job_id)


@router.post("/iam/benchmark")
async def iam_benchmark(body: IAMBenchmarkRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Run IAM benchmark synchronously — measures API latency / concurrency.

    This endpoint is synchronous (no background job) because it is fast
    and interactive.  It captures stdout from ``run_benchmark`` and
    returns it as text alongside the input parameters.
    """
    source = ConnectionService.get(db, body.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source connection not found")

    from aap_migration.api.crypto import decrypt_token

    token = decrypt_token(source.token) if getattr(source, "token", None) else ""
    if not token:
        raise HTTPException(status_code=400, detail="Source connection has no token")

    # Capture printed benchmark output
    buf = io.StringIO()
    import contextlib

    try:
        from aap_migration.iam.benchmark import run_benchmark

        with contextlib.redirect_stdout(buf):
            run_benchmark(
                source_url=source.url,
                source_token=token,
                verify_ssl=body.verify_ssl,
                sample_size=body.sample_size,
                worker_counts=body.workers or [1, 10, 20],
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Benchmark failed: {exc}") from exc

    return {
        "source_id": body.source_id,
        "source_url": source.url,
        "sample_size": body.sample_size,
        "workers": body.workers or [1, 10, 20],
        "output": buf.getvalue(),
    }


@router.get("/iam/{job_id}")
def get_iam_result(job_id: str) -> dict[str, Any]:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    data = job.to_dict()
    if job.status == JobStatus.COMPLETED and job.result:
        data["data"] = job.result
    return data


@router.get("/iam/{job_id}/export/json")
def export_iam_json(job_id: str) -> Response:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.COMPLETED or job.result is None:
        raise HTTPException(status_code=400, detail="IAM job not yet complete")

    payload = job.result.get("result") if isinstance(job.result, dict) and "result" in job.result else job.result
    content = json.dumps(payload, indent=2, default=str)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="iam-{job_id}.json"'},
    )


@router.get("/iam/{job_id}/export/html")
def export_iam_html(job_id: str) -> HTMLResponse:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="IAM job not yet complete")

    html = getattr(job, "_html_report", None)
    if html is None:
        # Fallback: generate HTML from stored result
        try:
            from aap_migration.iam.models import IAMAuditResult
            from aap_migration.iam.report import generate_iam_html_report

            payload = job.result.get("result") if isinstance(job.result, dict) and "result" in job.result else job.result
            if isinstance(payload, dict) and "metadata" in payload:
                # Rehydrate minimal result
                from aap_migration.iam.models import MigrationStats

                stats_raw = payload.get("statistics", {})
                stats = MigrationStats(**{k: v for k, v in stats_raw.items() if k in MigrationStats.__dataclass_fields__})
                result = IAMAuditResult(
                    mode=payload.get("metadata", {}).get("mode", "audit"),
                    source_url=payload.get("metadata", {}).get("source_url", ""),
                    stats=stats,
                )
                # Attach raw permissions etc. for report generation if present
                raw_perms = payload.get("permissions", [])
                if raw_perms:
                    from aap_migration.iam.models import PermissionEntry

                    result.permissions = [PermissionEntry.from_dict(p) for p in raw_perms]
                html = generate_iam_html_report(result)
            else:
                raise ValueError("No result payload")
        except Exception:
            raise HTTPException(status_code=400, detail="HTML report not available — re-run IAM job")

    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": f'attachment; filename="iam-{job_id}.html"'},
    )


@router.post("/iam/report", response_model=dict)
async def iam_regenerate_report(body: IAMReportRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Regenerate HTML report from a previous JSON export (by job_id or json_path).

    - If ``job_id`` is provided, loads job result from DB and re-renders HTML.
    - If ``json_path`` is provided, loads from filesystem (same as CLI report subcommand).
    """
    if body.job_id:
        svc = get_job_service()
        job = svc.get_job(body.job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.result is None:
            raise HTTPException(status_code=400, detail="Job has no result to regenerate from")
        # Export HTML via same endpoint logic
        html = getattr(job, "_html_report", None)
        if html is None:
            # Trigger fallback generation
            export_iam_html(body.job_id)
            job = svc.get_job(body.job_id)
            html = getattr(job, "_html_report", None)
        return {"job_id": body.job_id, "html_length": len(html) if html else 0}

    if body.json_path:
        try:
            from aap_migration.iam.report import generate_iam_html_report, load_audit_result_from_json

            result = load_audit_result_from_json(body.json_path)
            html = generate_iam_html_report(result)
            return {"json_path": body.json_path, "html_length": len(html)}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to load JSON: {exc}") from exc

    raise HTTPException(status_code=400, detail="Provide job_id or json_path")
