"""Job management endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from aap_migration.api.dependencies import get_job_service

router = APIRouter()


@router.get("/jobs")
def list_jobs() -> list[dict[str, Any]]:
    svc = get_job_service()
    return svc.list_jobs()


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, str]:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    cancelled = svc.cancel_job(job_id)
    return {"status": "cancelled" if cancelled else job.status}
