"""Job management endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from aap_migration.api.dependencies import get_job_service, verify_api_token
from aap_migration.api.services.job_service import JobService

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(verify_api_token)])


@router.get("", response_model=list[dict[str, Any]])
async def list_jobs(job_service: JobService = Depends(get_job_service)) -> list[dict[str, Any]]:
    return job_service.list_jobs()


@router.get("/{job_id}", response_model=dict[str, Any])
async def get_job(
    job_id: str, job_service: JobService = Depends(get_job_service)
) -> dict[str, Any]:
    data = job_service.get_job_dict(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    return data


@router.post("/{job_id}/cancel", response_model=dict[str, str])
async def cancel_job(
    job_id: str, job_service: JobService = Depends(get_job_service)
) -> dict[str, str]:
    ok = await job_service.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or not cancellable")
    return {"status": "cancelled"}


@router.post("/{job_id}/resume", response_model=dict[str, str])
async def resume_job(
    job_id: str, job_service: JobService = Depends(get_job_service)
) -> dict[str, str]:
    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status.value != "waiting_for_input":
        raise HTTPException(
            status_code=400, detail=f"Job is not waiting for input (status: {job.status.value})"
        )
    ok = await job_service.resume_job(job_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Cannot resume job")
    return {"status": "resumed"}
