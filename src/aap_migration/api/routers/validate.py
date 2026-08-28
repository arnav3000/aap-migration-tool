"""Validate API (Task 5 clean)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, get_job_service, verify_api_token
from aap_migration.api.schemas import JobStartResponse, ValidateRunRequest
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.validate_service import ValidateService

router = APIRouter(prefix="/validate", tags=["validate"], dependencies=[Depends(verify_api_token)])


@router.post("/run", response_model=JobStartResponse)
async def validate_run(body: ValidateRunRequest, db: Session = Depends(get_db)) -> JobStartResponse:
    src = ConnectionService.get(db, body.source_id) if body.source_id else None
    tgt = ConnectionService.get(db, body.destination_id) if body.destination_id else None
    if body.source_id and not src:
        raise HTTPException(status_code=404, detail="Source connection not found")
    if body.destination_id and not tgt:
        raise HTTPException(status_code=404, detail="Target connection not found")
    if body.resource_type:
        from aap_migration.resources import RESOURCE_REGISTRY

        if body.resource_type not in RESOURCE_REGISTRY:
            raise HTTPException(
                status_code=422, detail=f"Unknown resource type: {body.resource_type}"
            )
    svc = ValidateService(job_service=get_job_service())
    job_id = await svc.start_validate(
        src,
        tgt,
        live=body.live,
        resource_type=body.resource_type,
        skip_hosts=body.skip_hosts,
        organizations=body.organizations,
    )
    job = get_job_service().get_job(job_id)
    return JobStartResponse(job_id=job_id, seq_id=job.seq_id if job else None)


@router.get("/{job_id}")
async def get_validate(job_id: str) -> dict[str, Any]:
    svc = get_job_service()
    data = svc.get_job_dict(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Validate job not found")
    return {
        "job_id": job_id,
        "status": data.get("status"),
        "result": data.get("result"),
        "error": data.get("error"),
    }


@router.get("/{job_id}/export/json")
async def export_validate_json(job_id: str) -> Response:
    svc = get_job_service()
    data = svc.get_job_dict(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Validate job not found")
    if data.get("status") != "completed":
        raise HTTPException(
            status_code=409, detail=f"Job not completed (status={data.get('status')})"
        )
    result = data.get("result") or {}
    content = json.dumps(result, indent=2)
    return Response(content=content, media_type="application/json")


@router.get("/{job_id}/export/html")
async def export_validate_html(job_id: str) -> HTMLResponse:
    svc = get_job_service()
    data = svc.get_job_dict(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Validate job not found")
    if data.get("status") != "completed":
        raise HTTPException(
            status_code=409, detail=f"Job not completed (status={data.get('status')})"
        )
    result = data.get("result") or {}
    html = result.get("html") if isinstance(result, dict) else None
    if not html:
        html = (
            "<html><body><h1>Validation</h1><pre>"
            + json.dumps(result, indent=2)
            + "</pre></body></html>"
        )
    return HTMLResponse(content=html)
