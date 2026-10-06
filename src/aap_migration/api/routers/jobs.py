"""Job polling endpoints."""

from __future__ import annotations

import os
from bisect import bisect_right

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from aap_migration.api._errors import _key_detail
from aap_migration.api.jobs import JobRecord, get_job_manager, read_console_tail
from aap_migration.api.jobs._records import public_job_params
from aap_migration.api.routers._common import _narrow_status
from aap_migration.api.schemas import (
    DeleteJobOut,
    JobArtifactsOut,
    JobCancelOut,
    JobConsoleOut,
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
        params=public_job_params(dict(payload.get("params", {}))),
        result=payload.get("result"),
        error=payload.get("error"),
        exit_code=payload.get("exit_code"),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
    )


def _list_artifacts_page(
    job_dir: str, limit: int = 500, offset: int = 0, after: str | None = None
) -> tuple[list[str], int, int, bool]:
    """Single-pass paginated artifact listing with true total.

    One os.walk collects matching paths once (same exclusions as the
    listing), sorts them, then slices the requested page and counts the
    true pre-page total. Returns (page, true_total, walked, truncated)
    where walked is the bounded-walk count (limit+offset+1 cap, plus any
    cursor skip) and truncated signals incompleteness. Single traversal
    halves directory I/O per request versus separate filtered + count
    walks on repeatedly polled endpoints.

    ``after`` is an exclusive cursor (a relative path from a previous
    page, usually its last item): paging resumes after it instead of at
    an absolute offset, so files created while the job writes cannot
    shift offsets and silently skip entries. Prefer it over ``offset``
    when polling a running job; ``offset`` then applies post-cursor.
    ``total`` is always the point-in-time snapshot count, so a growing
    job directory legitimately reports a larger total on the next page.
    """
    limit = max(1, limit)
    offset = max(0, offset)
    all_paths: list[str] = []
    for root, dirnames, files in os.walk(job_dir):
        dirnames.sort()
        for name in sorted(files):
            if name in _EXCLUDED_ARTIFACTS or name.endswith(_EXCLUDED_SUFFIXES):
                continue
            full = os.path.join(root, name)
            all_paths.append(os.path.relpath(full, job_dir))
    # Explicit global sort: walk order (root files before subdirs) is not
    # lexicographic, and the cursor relies on sorted order.
    all_paths.sort()
    # Pure string comparison (never a filesystem lookup), so a missing or
    # adversarial anchor resolves to its insertion point, never an error.
    start = bisect_right(all_paths, after) if after else 0
    window = all_paths[start:]
    true_total = len(all_paths)
    # A page holds window[offset:offset+limit]; it is incomplete exactly
    # when the window exceeds offset+limit. The +1 probe only bounds the
    # walked count, never the incompleteness decision: at the exact
    # boundary the page hides one item.
    if len(window) > offset + limit:
        walked = start + offset + limit + 1
        truncated = True
    else:
        walked = true_total
        truncated = False
    page = window[offset : offset + limit]
    return page, true_total, walked, truncated


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
    pre-page count) and ``walked`` (bounded-walk count, limit+offset+1
    cap). v1 freezes one meaning per field. List endpoints cap ``limit``
    at 1000 (artifacts/console tails allow 5000); shared pagers must
    clamp per route.
    """
    manager = get_job_manager()
    total = manager.count(status)
    jobs = manager.list_jobs(status, limit, offset)
    items = [_to_status(j) for j in jobs]
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


@router.get("/jobs/{job_id}/console", response_model=JobConsoleOut)
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
    after: str | None = Query(
        default=None,
        description="Exclusive cursor: resume after this relative path "
        "(usually the previous page's last item). Prefer over offset when "
        "polling a running job; offset then applies post-cursor.",
    ),
) -> dict:
    """List artifact files produced in a job directory (paginated).

    Pair with ``GET /jobs/{job_id}/artifacts/{path}`` to download a
    listed file.

    ``total`` is the true pre-page count of this snapshot (a growing job
    directory legitimately reports a larger total on the next request);
    ``walked`` is the bounded-walk count so far and ``truncated`` alone
    signals incompleteness (the window beyond the cursor exceeds
    offset+limit). v1 freezes one meaning per field (see
    ``JobArtifactsOut``). List endpoints (jobs, connections, mappings,
    checkpoints) cap ``limit`` at 1000; artifact/console tails allow up
    to 5000.
    """
    try:
        job = get_job_manager().get_internal(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_key_detail(exc)) from exc
    # Single-pass paginated listing with true total (no double walk).
    page, true_total, walked, truncated = _list_artifacts_page(
        job["job_dir"], limit=limit, offset=offset, after=after
    )
    payload: dict = {
        "job_id": job_id,
        "artifacts": page,
        "walked": walked,
        "total": true_total,
        "truncated": truncated,
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
        # Generic detail (CWE-209): confine_path's message embeds the
        # resolved absolute base dir; echoing it would disclose server
        # filesystem layout to any token holder (matches iam-report).
        raise HTTPException(
            status_code=400, detail="artifact_path must stay under the job directory"
        ) from exc
    name = confined.name
    if name in _EXCLUDED_ARTIFACTS or name.endswith(_EXCLUDED_SUFFIXES):
        raise HTTPException(status_code=404, detail="Artifact not found")
    if not confined.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    # Single-open capped read: the size gate and the bytes come from one
    # file object, so appends landing between a stat() and a read_bytes()
    # cannot defeat the 32 MiB cap (TOCTOU). Reads cap+1 bytes in chunks
    # and 413s as soon as the cap is exceeded.
    try:
        chunks: list[bytes] = []
        seen = 0
        with open(confined, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                seen += len(chunk)
                if seen > _MAX_ARTIFACT_DOWNLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Artifact exceeds {_MAX_ARTIFACT_DOWNLOAD_BYTES} bytes; fetch it from the job directory",
                    )
                chunks.append(chunk)
        content = b"".join(chunks)
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


@router.post("/jobs/{job_id}/cancel", response_model=JobCancelOut)
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
    # Narrow through the same literal poll uses so a future manager status
    # fails loudly as 500 here too instead of leaking a raw string.
    return {
        "job_id": job_id,
        "status": _narrow_status(job["status"]),
        "cancel_pending": job["status"] == "running",
    }


@router.delete("/jobs/{job_id}", response_model=DeleteJobOut)
def delete_job(job_id: str) -> dict:
    """Delete a terminal job record (and its on-disk directory)."""
    try:
        get_job_manager().delete(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_key_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted_job_id": job_id}
