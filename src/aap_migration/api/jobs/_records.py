"""Job record shapes and shared result normalization."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict

from aap_migration.api._errors import (
    ConflictError,
    InternalStatusError,
    QueueFullError,
    ServerShuttingDownError,
    StorageUnhealthyError,
    UnknownJobError,
    WorkdirGoneError,
    _key_detail,
    _store_http_error,
)

__all__ = [
    "ConflictError",
    "InternalStatusError",
    "QueueFullError",
    "ServerShuttingDownError",
    "StorageUnhealthyError",
    "UnknownJobError",
    "WorkdirGoneError",
    "_key_detail",
    "_store_http_error",
]

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
    # Set when cancel arrives for a queued job (pollable pending-cancel
    # signal for future resume/retry phases). Optional.
    cancel_requested_at: str
    # Set when a running job finishes an attempt after cancel_requested:
    # the phase where it stopped plus phases already applied, so the
    # cancel fence can refuse non-force chained resubmissions that would
    # replay those writes. Optional.
    cancelled_at_phase: str
    completed_phases: list[str]


class JobUpdate(TypedDict, total=False):
    """Constrained write shape for ``JobManager._set`` (P2 #9).

    Mirrors every key the manager/worker writes so a misspelled or
    undeclared key is a type error instead of silent missing poll data.
    ``_set`` takes ``**fields: Unpack[JobUpdate]`` and needs no
    ``type: ignore``. ``job_id`` is the positional target, never an
    update field, so it is deliberately absent here (mypy rejects the
    overlap).
    """

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
    shutdown_interrupted: bool
    cancel_requested_at: str
    cancelled_at_phase: str
    completed_phases: list[str]


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
