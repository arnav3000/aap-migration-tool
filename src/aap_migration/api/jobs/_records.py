"""Job record shapes and shared result normalization."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")

# Non-terminal statuses: queued or running work still owns its workdir,
# connections, and state DB. Single home for the membership test so a
# future status lands in one place instead of six literal tuples.
ACTIVE_STATUSES = ("queued", "running")


class JobRecord(TypedDict, total=False):
    """Fixed shape for every job record (internal + public)."""

    job_id: str
    job_type: str
    status: str
    params: dict[str, Any]
    job_dir: str
    result: dict[str, Any] | None
    error: str | None
    error_id: str | None
    exit_code: int | None
    created_at: str
    updated_at: str
    cancel_requested: bool
    # Set by shutdown_drain when leftover work is transitioned (P2 #19):
    # the worker must not overwrite the interrupted verdict if the pool
    # thread finishes afterwards. Optional (older records lack it).
    shutdown_interrupted: bool


class QueueFullError(RuntimeError):
    """Raised when the FIFO queue is at capacity (mapped to HTTP 429)."""


class StorageUnhealthyError(QueueFullError):
    """Storage probes failed at startup (mapped to HTTP 503)."""


class ServerShuttingDownError(QueueFullError):
    """Submissions closed during drain (mapped to HTTP 503)."""


class InternalStatusError(RuntimeError):
    """Unknown internal job status (mapped to HTTP 500, never 400)."""


class ConflictError(ValueError):
    """Lifecycle conflict: queued/running work blocks the mutation (HTTP 409)."""


class UnknownJobError(KeyError):
    """Unknown job id (HTTP 404). Carries the job id for message stability."""


class WorkdirGoneError(KeyError):
    """Referenced job's working directory no longer exists (HTTP 404).

    Raised by ``context.resolve_workdir`` instead of a message-sniffed
    ValueError so every router maps it through the shared type-based
    mapper: rewording the message can never flip the wire contract
    (see P2 #20).
    """


def public_job_params(params: dict[str, Any]) -> dict[str, Any]:
    """Single home for the client-visible job-params rule.

    Internal ``_*`` keys (submit-time ``_snapshot_*`` pins) stay
    server-side. Both the manager's ``_public`` view and the routers'
    job views call this so the views cannot diverge.
    """
    return {k: v for k, v in params.items() if not k.startswith("_")}


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_result(result: Any) -> Any:
    """Give every worker payload the shared message/artifacts envelope.

    Workers return per-type keys; consumers must not branch on undocumented
    shapes. Success dict payloads always carry ``message`` (falling back to
    ``status`` or a generic complete message) and ``artifacts`` (possibly
    empty) after this point, including coordinator payloads that previously
    returned bare (e.g. credential compare/migrate with no message key).
    """
    if isinstance(result, dict):
        if "message" not in result:
            fallback = result.get("status") or "Complete"
            result = {**result, "message": str(fallback)}
        if "artifacts" not in result:
            return {**result, "artifacts": []}
    return result


def _key_detail(exc: BaseException) -> str:
    """Unwrap KeyError to a bare string (no repr quotes) for 404 details."""
    args = getattr(exc, "args", ())
    if args:
        first = args[0]
        if isinstance(first, str):
            return first
        return str(first)
    text = str(exc).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _store_http_error(exc: Exception) -> HTTPException:
    """Single home for store/manager error -> HTTP mapping (type-based).

    Lives here (next to the exception types) so foundation modules
    (context, jobs) map errors without importing the future routers
    layer. Codes come from exception types only, never message text:
    rewording a message cannot flip the wire contract.
    """
    if isinstance(exc, UnknownJobError) or isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=_key_detail(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, IntegrityError):
        # Concurrent unique races (two creates slipping past the SELECT
        # pre-check) surface here, not as unhandled 500s. Never leak SQL
        # text: the detail names the likely cause only.
        return HTTPException(
            status_code=409,
            detail="Resource conflict: a record with the same unique value "
            "already exists; retry with a unique value",
        )
    if isinstance(exc, StorageUnhealthyError | ServerShuttingDownError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, QueueFullError):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, InternalStatusError):
        return HTTPException(status_code=500, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc  # pragma: no cover
