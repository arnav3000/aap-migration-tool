"""Job record shapes and shared result normalization."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")

# Non-terminal statuses: queued or running work still owns its workdir,
# connections, and state DB. Single home for the membership test so a
# future status lands in one place instead of six literal tuples.
ACTIVE_STATUSES = ("queued", "running")

API_V1_PREFIX = "/api/v1"


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


class QueueFullError(RuntimeError):
    """Raised when the FIFO queue is at capacity (mapped to HTTP 429)."""


class ConflictError(ValueError):
    """Lifecycle conflict: queued/running work blocks the mutation (HTTP 409)."""


class UnknownJobError(KeyError):
    """Unknown job id (HTTP 404). Carries the job id for message stability."""


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
