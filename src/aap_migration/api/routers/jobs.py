"""Job management endpoints."""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

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


@router.post("/jobs/{job_id}/resume")
def resume_job(job_id: str) -> dict[str, str]:
    """Resume a job that is paused waiting for user input (e.g. credential update)."""
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "waiting_for_input":
        raise HTTPException(
            status_code=400,
            detail=f"Job is not waiting for input (status: {job.status})",
        )
    resumed = svc.resume_job(job_id)
    return {"status": "running" if resumed else job.status}


@router.get("/jobs/{job_id}/credentials")
def get_job_credentials(job_id: str) -> list[dict[str, Any]]:
    """Return the credential review list for a migration job."""
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.result or "credential_review" not in job.result:
        return []
    cred_list: list[dict[str, Any]] = job.result["credential_review"]
    return cred_list


@router.get("/jobs/{job_id}/credentials.csv")
def get_job_credentials_csv(job_id: str) -> StreamingResponse:
    """Download credential review list as CSV."""
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    creds = (job.result or {}).get("credential_review", [])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["Credential Name", "Credential Type", "Organization", "Used By Type", "Used By Name"]
    )
    for cred in creds:
        for usage in cred.get("used_by", []):
            writer.writerow(
                [
                    cred.get("name", ""),
                    cred.get("credential_type", ""),
                    cred.get("organization", ""),
                    usage.get("resource_type", ""),
                    usage.get("resource_name", ""),
                ]
            )
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=credentials-{job_id}.csv"},
    )
