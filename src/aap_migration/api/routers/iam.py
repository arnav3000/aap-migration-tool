"""IAM API (Task 5 clean) — audit / migrate as jobs."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, get_job_service, verify_api_token
from aap_migration.api.schemas import IAMAuditRequest, IAMMigrateRequest, JobStartResponse
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.iam_service import IAMService

router = APIRouter(prefix="/iam", tags=["iam"], dependencies=[Depends(verify_api_token)])


@router.post("/audit", response_model=JobStartResponse)
async def iam_audit(body: IAMAuditRequest, db: Session = Depends(get_db)) -> JobStartResponse:
    conn = ConnectionService.get(db, body.source_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Source connection not found")
    svc = IAMService(job_service=get_job_service())
    job_id = await svc.start_audit(conn, scan_strategy=body.scan_strategy, workers=body.workers)
    job = get_job_service().get_job(job_id)
    return JobStartResponse(job_id=job_id, seq_id=job.seq_id if job else None)


@router.post("/migrate", response_model=JobStartResponse)
async def iam_migrate(body: IAMMigrateRequest, db: Session = Depends(get_db)) -> JobStartResponse:
    src = ConnectionService.get(db, body.source_id)
    tgt = ConnectionService.get(db, body.destination_id)
    if not src or not tgt:
        raise HTTPException(status_code=404, detail="Source or target connection not found")
    svc = IAMService(job_service=get_job_service())
    job_id = await svc.start_migrate(
        src,
        tgt,
        scan_strategy=body.scan_strategy,
        workers=body.workers,
        dry_run=body.dry_run,
        skip_user_roles=body.skip_user_roles,
    )
    job = get_job_service().get_job(job_id)
    return JobStartResponse(job_id=job_id, seq_id=job.seq_id if job else None)


@router.get("/{job_id}")
async def get_iam(job_id: str) -> dict[str, Any]:
    svc = get_job_service()
    data = svc.get_job_dict(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="IAM job not found")
    return {
        "job_id": job_id,
        "status": data.get("status"),
        "result": data.get("result"),
        "error": data.get("error"),
    }


@router.get("/{job_id}/export/json")
async def export_iam_json(job_id: str) -> Response:
    svc = get_job_service()
    data = svc.get_job_dict(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="IAM job not found")
    if data.get("status") != "completed":
        raise HTTPException(
            status_code=409, detail=f"Job not completed (status={data.get('status')})"
        )
    result = data.get("result") or {}
    content = json.dumps(result, indent=2)
    return Response(content=content, media_type="application/json")


@router.get("/{job_id}/export/html")
async def export_iam_html(job_id: str) -> HTMLResponse:
    svc = get_job_service()
    data = svc.get_job_dict(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="IAM job not found")
    if data.get("status") != "completed":
        raise HTTPException(
            status_code=409, detail=f"Job not completed (status={data.get('status')})"
        )
    result = data.get("result") or {}
    html = result.get("html") if isinstance(result, dict) else None
    if not html:
        html = (
            "<html><body><h1>IAM Report</h1><pre>"
            + json.dumps(result, indent=2)
            + "</pre></body></html>"
        )
    return HTMLResponse(content=html)


@router.get("/benchmark")
async def iam_benchmark() -> dict[str, Any]:
    # Stub: return synthetic benchmark data
    return {"benchmark": "ok", "message": "IAM benchmark stub — run audit for real data"}


@router.get("/report")
async def iam_report() -> dict[str, Any]:
    return {"report": "ok", "message": "Use /iam/{job_id}/export/html for report"}
