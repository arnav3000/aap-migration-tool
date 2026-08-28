"""Validate endpoints — API wrapper around the shared validate engine.

CLI: ``src/aap_migration/cli/commands/validate.py`` (``run_validation``)
API: this router (same ``validate/runner.py:run_validation`` + ``validate/report.py``)

``UI optional but same codebase does migration same way using CLI or web`` —
validate/iam use the same engine via ``api/services/validate_service.py``.

Endpoints:
  POST /api/validate/run          -> start background validation (JobStartResponse)
  GET  /api/validate/{job_id}     -> job status + result summary
  GET  /api/validate/{job_id}/export/json  -> full ValidationResult JSON
  GET  /api/validate/{job_id}/export/html  -> self-contained HTML report
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, get_db_url, get_job_service
from aap_migration.api.schemas import JobStartResponse, ValidateRunRequest
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.job_service import Job, JobStatus
from aap_migration.api.services.validate_service import validate_job_coro_factory

router = APIRouter()


@router.post("/validate/run", response_model=JobStartResponse)
async def run_validate(body: ValidateRunRequest, db: Session = Depends(get_db)) -> JobStartResponse:
    """Start a post-migration validation job.

    - ``live=false`` (default): database-only mode — compares export counts
      against ``migration_progress`` / ``id_mappings`` (no target API calls).
    - ``live=true``: compares ``exports/`` against the live target API by
      identity (name / org / parent), using the state DB only to explain gaps.
    - ``organizations``: list of org names to scope (mirrors ``--orgs``).
    - ``resource_type`` / ``skip_hosts`` / ``organizations`` mirror CLI flags.

    When ``source_id`` / ``destination_id`` are provided the server uses
    those connections for report headers and live target fetching.  When
    omitted the server falls back to the host config / placeholder URLs.
    """
    if body.skip_hosts and body.resource_type == "hosts":
        raise HTTPException(status_code=400, detail="--skip-hosts conflicts with resource_type=hosts")

    source_conn = None
    target_conn = None

    if body.source_id:
        source_conn = ConnectionService.get(db, body.source_id)
        if source_conn is None:
            raise HTTPException(status_code=404, detail="Source connection not found")

    if body.destination_id:
        target_conn = ConnectionService.get(db, body.destination_id)
        if target_conn is None:
            raise HTTPException(status_code=404, detail="Destination connection not found")

    if body.live and body.destination_id is None:
        # Allow live without explicit destination_id only if a single target
        # connection exists in the DB (quality-of-life for single-target envs).
        # Otherwise require explicit destination_id for clarity.
        all_conns = ConnectionService.list_all(db)
        targets = [c for c in all_conns if c.role in ("destination", "target")]
        if len(targets) == 1:
            target_conn = targets[0]
        elif body.source_id is None:
            # No way to know which target to hit — caller must specify
            pass  # validate_service will build a placeholder client and run_validation will error clearly

    svc = get_job_service()
    db_url = get_db_url()
    # In tests the API uses a file-backed sqlite DB but MIGRATION_STATE_DB_PATH
    # defaults to postgres — prefer the actual DB bound to this request when it
    # is sqlite so validate can reuse the same state DB as the rest of the app.
    try:
        bind_url = str(db.get_bind().url)
        if bind_url.startswith("sqlite") and db_url.startswith("postgresql://"):
            db_url = bind_url
    except Exception:  # nosec B110
        pass

    coro_factory = validate_job_coro_factory(
        live=body.live,
        resource_type=body.resource_type,
        skip_hosts=body.skip_hosts,
        organizations=body.organizations,
        source_conn=source_conn,
        target_conn=target_conn,
        export_dir=None,
        output_dir=body.output_dir,
        db_url=db_url,
    )

    job_name = f"Validate {'live' if body.live else 'db'}"
    if body.resource_type:
        job_name += f" {body.resource_type}"
    if body.organizations:
        job_name += f" ({', '.join(body.organizations[:3])})"

    job_id = svc.start_job(job_name, "validate", coro_factory)
    return JobStartResponse(job_id=job_id)


@router.get("/validate/{job_id}")
def get_validate_result(job_id: str) -> dict[str, Any]:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    data = job.to_dict()
    if job.status == JobStatus.COMPLETED and job.result:
        data["data"] = job.result
    return data


@router.get("/validate/{job_id}/export/json")
def export_validate_json(job_id: str) -> Response:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.COMPLETED or job.result is None:
        raise HTTPException(status_code=400, detail="Validation not yet complete")

    # Stored result contains the ValidationResult dict under "result"
    payload = job.result.get("result") if isinstance(job.result, dict) and "result" in job.result else job.result
    content = json.dumps(payload, indent=2, default=str)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="validate-{job_id}.json"'},
    )


@router.get("/validate/{job_id}/export/html")
def export_validate_html(job_id: str) -> HTMLResponse:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Validation not yet complete")

    html = getattr(job, "_html_report", None)
    if html is None:
        # Fallback: regenerate from stored result if job was reloaded from DB
        result_payload = None
        if isinstance(job.result, dict):
            result_payload = job.result.get("result", job.result)
        if result_payload is None:
            raise HTTPException(status_code=400, detail="HTML report not available — re-run validation")
        # Minimal HTML fallback
        html = f"<html><body><pre>{json.dumps(result_payload, indent=2, default=str)[:20000]}</pre></body></html>"

    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": f'attachment; filename="validate-{job_id}.html"'},
    )
