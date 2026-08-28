"""Analysis API (Task 4 clean) — dependency analysis as background job."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, get_job_service, verify_api_token
from aap_migration.api.schemas import AnalysisRunRequest, JobStartResponse
from aap_migration.api.services.analysis_service import AnalysisService
from aap_migration.api.services.connection_service import ConnectionService

router = APIRouter(prefix="/analysis", tags=["analysis"], dependencies=[Depends(verify_api_token)])


@router.post("/run", response_model=JobStartResponse)
async def run_analysis(body: AnalysisRunRequest, db: Session = Depends(get_db)) -> JobStartResponse:
    conn = ConnectionService.get(db, body.connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    svc = AnalysisService(job_service=get_job_service())
    job_id = await svc.start_analysis(conn, organizations=body.organizations)
    # Retrieve seq_id
    job = get_job_service().get_job(job_id)
    seq_id = job.seq_id if job else None
    return JobStartResponse(job_id=job_id, seq_id=seq_id)


@router.get("/{job_id}")
async def get_analysis(job_id: str) -> dict[str, Any]:
    svc = get_job_service()
    data = svc.get_job_dict(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    if data.get("type") != "analysis" and data.get("status") not in (
        "running",
        "pending",
        "completed",
        "failed",
    ):
        # Allow but warn? keep strict: only analysis jobs
        # For flexibility, allow any job but check type
        pass
    return {
        "job_id": job_id,
        "status": data.get("status"),
        "result": data.get("result"),
        "error": data.get("error"),
        "output": data.get("output"),
    }


@router.get("/{job_id}/export/json")
async def export_analysis_json(job_id: str) -> Response:
    svc = get_job_service()
    data = svc.get_job_dict(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    if data.get("status") != "completed":
        raise HTTPException(
            status_code=409, detail=f"Job not completed (status={data.get('status')})"
        )
    result = data.get("result") or {}
    report = result.get("report") if isinstance(result, dict) else result
    if not report:
        report = result
    content = json.dumps(report, indent=2)
    return Response(content=content, media_type="application/json")


@router.get("/{job_id}/export/html")
async def export_analysis_html(job_id: str) -> HTMLResponse:
    svc = get_job_service()
    data = svc.get_job_dict(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    if data.get("status") != "completed":
        raise HTTPException(
            status_code=409, detail=f"Job not completed (status={data.get('status')})"
        )
    result = data.get("result") or {}
    html = result.get("html") if isinstance(result, dict) else None
    if not html:
        raise HTTPException(status_code=404, detail="HTML report not available")
    return HTMLResponse(content=html)
