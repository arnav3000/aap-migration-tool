"""Job polling endpoints."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from aap_migration.api.jobs import JobRecord, get_job_manager, read_console_tail
from aap_migration.api.routers._common import _key_detail, _narrow_status, public_params
from aap_migration.api.schemas import (
    JobArtifactsOut,
    JobListFilter,
    JobListOut,
    JobStatus,
)

router = APIRouter(tags=["jobs"])

_EXCLUDED_ARTIFACTS = {"config.yaml", "api_fernet.key"}
_EXCLUDED_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal")


def _to_status(payload: JobRecord) -> JobStatus:
    """Convert an internal job record to the public poll model.

    Drops the internal ``job_dir`` and narrows the status string to the
    ``JobStatusValue`` literal the schema requires.
    """
    return JobStatus(
        job_id=payload["job_id"],
        job_type=payload["job_type"],
        status=_narrow_status(payload["status"]),
        params=public_params(dict(payload.get("params", {}))),
        result=payload.get("result"),
        error=payload.get("error"),
        exit_code=payload.get("exit_code"),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
    )


def _filtered_artifacts(job_dir: str, limit: int = 500) -> tuple[list[str], bool]:
    """Walk a job dir (bounded) excluding secret-bearing files.

    Directory traversal is sorted for stable pagination; secret sidecars
    (``*.db*``, keys, configs) are excluded at any depth. Use
    ``GET /jobs/{id}/artifacts/{path}`` to download a listed file.
    """
    artifacts: list[str] = []
    truncated = False
    count = 0
    for root, dirnames, files in os.walk(job_dir):
        dirnames.sort()
        for name in sorted(files):
            if name in _EXCLUDED_ARTIFACTS or name.endswith(_EXCLUDED_SUFFIXES):
                continue
            full = os.path.join(root, name)
            artifacts.append(os.path.relpath(full, job_dir))
            count += 1
            if count >= limit:
                truncated = True
                return artifacts, truncated
    return artifacts, truncated


def _count_all_artifacts(job_dir: str) -> int:
    """True pre-page artifact count (full walk, same exclusions).

    P2 #11: ``total`` must be the true pre-page count everywhere; the
    bounded walk count stays in ``walked``. Removed in v2: the old
    bounded meaning of ``total`` (equal to ``walked``).
    """
    count = 0
    for _root, dirnames, files in os.walk(job_dir):
        dirnames.sort()
        for name in sorted(files):
            if name in _EXCLUDED_ARTIFACTS or name.endswith(_EXCLUDED_SUFFIXES):
                continue
            count += 1
    return count


@router.get("/jobs", response_model=JobListOut)
def list_jobs(
    status: JobListFilter | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=100000),
) -> dict:
    """List background jobs, newest first (offset paginated).

    Always returns the ``{"items": [...], "total": N, "limit": L,
    "offset": O}`` envelope where ``total`` is the true pre-page count,
    matching the ``limit``/``offset``/``total`` keys on the
    state/checkpoint list endpoints. (The legacy bare-list shape was
    removed: one route, one shape.)

    Note: ``GET /jobs/{id}/artifacts`` returns both ``total`` (true
    pre-page count) and ``walked`` (bounded-walk count); ``total`` carries
    deprecated=True during the v2 transition (previously bounded). List
    endpoints cap ``limit`` at 1000 (artifacts/console tails allow 5000);
    shared pagers must clamp per route.
    """
    manager = get_job_manager()
    total = manager.count(status)
    jobs = manager.list_jobs(status, limit + offset)
    page = jobs[offset : offset + limit]
    items = [_to_status(j) for j in page]
    return {
        "items": [item.model_dump(mode="json") for item in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str) -> JobStatus:
    """Poll a background job."""
    try:
        return _to_status(get_job_manager().get(job_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_key_detail(exc)) from exc


@router.get("/jobs/{job_id}/console")
def get_job_console(job_id: str, tail: int = Query(default=100, ge=1, le=5000)) -> dict:
    """Return captured console output for a job (click echo output)."""
    try:
        job = get_job_manager().get_internal(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_key_detail(exc)) from exc
    content = read_console_tail(job["job_dir"], job_id=job_id)
    if not content:
        return {"job_id": job_id, "console": "", "console_available": False}
    lines = content.splitlines()
    return {
        "job_id": job_id,
        "console": "\n".join(lines[-tail:]),
        "console_available": True,
    }


@router.get("/jobs/{job_id}/artifacts", response_model=JobArtifactsOut)
def get_job_artifacts(
    job_id: str,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0, le=100000),
) -> dict:
    """List artifact files produced in a job directory (paginated).

    Pair with ``GET /jobs/{job_id}/artifacts/{path}`` to download a
    listed file.

    ``total`` is the true pre-page count (full filtered walk, matching
    ``GET /jobs`` and list endpoints); ``walked`` is the bounded-walk
    count so far (``limit+offset+1`` cap) and ``truncated`` alone signals
    incompleteness (true total exceeds walked). ``total`` was previously a
    bounded alias of ``walked``; bounded meaning removed in v2 (see
    ``JobArtifactsOut``: ``total`` carries deprecated=True during the
    transition, new clients read ``walked`` for bounded progress). List
    endpoints (jobs, connections, mappings, checkpoints) cap ``limit`` at
    1000; artifact/console tails allow up to 5000.
    """
    try:
        job = get_job_manager().get_internal(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_key_detail(exc)) from exc
    artifacts, truncated = _filtered_artifacts(job["job_dir"], limit=limit + offset + 1)
    page = artifacts[offset : offset + limit]
    # P2 #11: true pre-page total via full walk; walked stays bounded.
    # Removed in v2: old bounded meaning of total (equal to walked).
    true_total = _count_all_artifacts(job["job_dir"])
    walked = len(artifacts)
    payload: dict = {
        "job_id": job_id,
        "artifacts": page,
        "walked": walked,
        "total": true_total,
        "truncated": truncated or true_total > walked,
        "limit": limit,
        "offset": offset,
    }
    return payload


_MAX_ARTIFACT_DOWNLOAD_BYTES = 32 << 20


@router.get("/jobs/{job_id}/artifacts/{artifact_path:path}")
def download_job_artifact(job_id: str, artifact_path: str) -> Response:
    """Download one artifact file from a job directory.

    The path must be listed by ``GET /jobs/{job_id}/artifacts``: confined
    to the job directory (``..`` traversal, symlink escape, and absolute
    paths outside the dir are 400/404) and never a secret sidecar
    (``config.yaml``, ``*.db*``, keys return 404 like the listing).
    Downloads are capped at 32 MiB (413 beyond) so one report fetch
    cannot exhaust worker memory.
    """
    from aap_migration.api.security import confine_path

    try:
        job = get_job_manager().get_internal(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_key_detail(exc)) from exc
    try:
        confined = confine_path(artifact_path, job["job_dir"], label="artifact_path")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    name = confined.name
    if name in _EXCLUDED_ARTIFACTS or name.endswith(_EXCLUDED_SUFFIXES):
        raise HTTPException(status_code=404, detail="Artifact not found")
    if not confined.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    try:
        if confined.stat().st_size > _MAX_ARTIFACT_DOWNLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Artifact exceeds {_MAX_ARTIFACT_DOWNLOAD_BYTES} bytes; fetch it from the job directory",
            )
        content = confined.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    suffix = confined.suffix.lower()
    media_type = {
        ".json": "application/json",
        ".md": "text/markdown",
        ".html": "text/html",
        ".csv": "text/csv",
        ".txt": "text/plain",
        ".log": "text/plain",
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
    }.get(suffix, "application/octet-stream")
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """Request cancellation of a queued (or running) job.

    Queued jobs transition to ``cancelled`` immediately. Running jobs are
    flagged cancel-requested and keep status ``running`` until the worker
    observes the flag between steps: the 200 response carries
    ``cancel_pending: true`` in that case, so callers must re-poll
    ``GET /jobs/{id}`` until the terminal status lands instead of treating
     the 200 as proof of cancellation.
    """
    try:
        job = get_job_manager().cancel(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_key_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # One shape always: cancel_pending is True only while the worker still
    # owns the job (running); queued cancels settle to cancelled immediately
    # (False), so clients never KeyError on the common path.
    return {"job_id": job_id, "status": job["status"], "cancel_pending": job["status"] == "running"}


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    """Delete a terminal job record (and its on-disk directory)."""
    try:
        get_job_manager().delete(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_key_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted": job_id}
