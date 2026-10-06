"""Shared API error types and HTTP mapping (neutral home, P2 #17).

These exception types are raised by the store, jobs, context, and
security modules. They live here -- depending on nothing internal --
so every module imports them at top level instead of through
underscore-private ``jobs._records`` via deferred function-level
imports. ``jobs._records`` re-exports them for backward compatibility.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError


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

    Codes come from exception types only, never message text:
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
