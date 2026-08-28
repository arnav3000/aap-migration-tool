"""Operations API (Task 4 clean) — export / cleanup."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, get_job_service, verify_api_token
from aap_migration.api.schemas import CleanupRequest, ExportRequest, JobStartResponse
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.operation_service import OperationService
from aap_migration.resources import RESOURCE_REGISTRY

router = APIRouter(
    prefix="/operations", tags=["operations"], dependencies=[Depends(verify_api_token)]
)


@router.post("/export", response_model=JobStartResponse)
async def export_resources(body: ExportRequest, db: Session = Depends(get_db)) -> JobStartResponse:
    conn = ConnectionService.get(db, body.connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    if body.resource_types:
        for rt in body.resource_types:
            if rt not in RESOURCE_REGISTRY:
                raise HTTPException(status_code=422, detail=f"Unknown resource type: {rt}")
    svc = OperationService(
        job_service=get_job_service(), session_factory=get_job_service().db_session_factory
    )
    job_id = await svc.start_export(conn, resource_types=body.resource_types)
    job = get_job_service().get_job(job_id)
    return JobStartResponse(job_id=job_id, seq_id=job.seq_id if job else None)


@router.post("/cleanup", response_model=JobStartResponse)
async def cleanup_resources(
    body: CleanupRequest, db: Session = Depends(get_db)
) -> JobStartResponse:
    conn = ConnectionService.get(db, body.connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    if not body.resource_types:
        raise HTTPException(status_code=422, detail="resource_types required")
    for rt in body.resource_types:
        if rt not in RESOURCE_REGISTRY:
            raise HTTPException(status_code=422, detail=f"Unknown resource type: {rt}")
    svc = OperationService(
        job_service=get_job_service(), session_factory=get_job_service().db_session_factory
    )
    job_id = await svc.start_cleanup(conn, resource_types=body.resource_types)
    job = get_job_service().get_job(job_id)
    return JobStartResponse(job_id=job_id, seq_id=job.seq_id if job else None)


@router.get("/export/{job_id}")
async def get_export(job_id: str) -> dict[str, Any]:
    svc = get_job_service()
    data = svc.get_job_dict(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Export job not found")
    return {
        "job_id": job_id,
        "status": data.get("status"),
        "result": data.get("result"),
        "error": data.get("error"),
    }
