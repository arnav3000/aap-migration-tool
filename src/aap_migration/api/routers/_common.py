"""Shared router helpers: error mapping, job submission, pre-validation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, cast

from fastapi import HTTPException

from aap_migration.api._paths import API_V1_PREFIX
from aap_migration.api.jobs import (
    ConflictError,
    InternalStatusError,
    JobRecord,
    QueueFullError,
    ServerShuttingDownError,
    StorageUnhealthyError,
    UnknownJobError,
    get_job_manager,
)
from aap_migration.api.schemas import (
    ChainedRequest,
    ConnectionSelector,
    IamBenchmarkRequest,
    IamReportRequest,
    JobCreated,
    JobStatusValue,
)
from aap_migration.api.store import (
    SNAPSHOT_SOURCE_ID,
    SNAPSHOT_TARGET_ID,
    needs_connections,
    needs_target,
)


def _narrow_status(value: str) -> JobStatusValue:
    """Narrow an internal status string to the public literal (fail loudly).

    An unknown manager status is a server invariant violation: it raises
    InternalStatusError (500), never a client 400, so operators alerting
    on 5xx see it instead of clients retrying identical requests forever.
    """
    from typing import get_args

    if value not in get_args(JobStatusValue):
        raise InternalStatusError(f"Unknown job status '{value}'")
    return cast(JobStatusValue, value)


def public_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return the client-visible subset of stored job params.

    Internal ``_*`` keys (submit-time ``_snapshot_*`` pins written by
    ``submit_chained``) stay server-side: they pin execution-time
    connection resolution, not client input, and must never leak via job
    polling. Single home for the rule (mirrors the manager's ``_public``
    stripping) so every public job-params view filters the same way.
    """
    from aap_migration.api.jobs._records import public_job_params

    return public_job_params(params)


def _poll_url(job_id: str, root_path: str = "") -> str:
    """Build a root_path-aware poll URL for subpath-mounted deployments."""
    prefix = (root_path or "").rstrip("/")
    return f"{prefix}{API_V1_PREFIX}/jobs/{job_id}"


def submit_job(
    job_type: str,
    params: dict[str, Any],
    func: Callable[[JobRecord], dict[str, Any]],
    job_dir: str | None = None,
    root_path: str = "",
) -> JobCreated:
    """Enqueue a background job and build the response model."""
    try:
        job = get_job_manager().submit(job_type, params, func, job_dir=job_dir)
    except QueueFullError as exc:
        raise _store_http_error(exc) from exc
    chained_from = None
    if params.get("job_id"):
        try:
            chained_from = _narrow_status(get_job_manager().get(params["job_id"])["status"])
        except KeyError:
            chained_from = None
    return JobCreated(
        job_id=job["job_id"],
        job_type=job["job_type"],
        status=_narrow_status(job["status"]),
        poll_url=_poll_url(job["job_id"], root_path),
        chained_from_status=chained_from,
    )


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

    Codes come from exception types only, never message text: rewording a
    message cannot flip the wire contract.
    """
    if isinstance(exc, UnknownJobError) or isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=_key_detail(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, StorageUnhealthyError | ServerShuttingDownError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, QueueFullError):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, InternalStatusError):
        return HTTPException(status_code=500, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc  # pragma: no cover


def handle_store_errors(func: Callable, *args: Any, **kwargs: Any) -> Any:
    """Map store-layer errors to HTTP errors (single home, type-based)."""
    try:
        return func(*args, **kwargs)
    except (KeyError, ValueError, QueueFullError, InternalStatusError) as exc:
        raise _store_http_error(exc) from exc


def require_connections(
    source_id: str | None = None,
    target_id: str | None = None,
    need: Literal["both", "source", "none"] = "both",
) -> None:
    """Fail fast (400/404) when required stored connections are missing."""
    from aap_migration.api import store

    try:
        if not needs_connections(need):
            return
        if not needs_target(need):
            active = store.get_active()
            sid = source_id or active["source_id"]
            if not sid:
                raise ValueError(
                    "No source AAP configured. Create one via "
                    "POST /api/v1/connections and select it via "
                    "POST /api/v1/connections/active (or pass source_id)."
                )
            store.get_connection(sid, include_token=True)
        else:
            store.resolve_active_pair(source_id, target_id)
    except (KeyError, ValueError, QueueFullError, InternalStatusError) as exc:
        raise _store_http_error(exc) from exc


def _body_source_target(body: Any) -> tuple[str | None, str | None]:
    # ChainedRequest subclasses ConnectionSelector; one check covers both.
    if isinstance(body, ConnectionSelector):
        return body.source_id, body.target_id
    return getattr(body, "source_id", None), getattr(body, "target_id", None)


def _body_job_id(body: Any) -> str | None:
    if isinstance(body, ChainedRequest):
        return body.job_id
    return getattr(body, "job_id", None)


def submit_chained(
    job_type: str,
    body: ChainedRequest | ConnectionSelector | IamBenchmarkRequest | IamReportRequest,
    func: Callable[[JobRecord], dict[str, Any]],
    need: Literal["both", "source", "none"] = "both",
    allow_statuses: tuple[str, ...] = ("succeeded",),
    root_path: str = "",
) -> JobCreated:
    """Pre-validate connections + chaining, then enqueue the job.

    Submit-time checks mirror execution-time resolution, and the effective
    pair is fingerprinted into stored params (``_snapshot_*``) so workers
    fail fast when connections drift between submit (202) and execution
    instead of silently switching credentials or targets. Pair switches
    mid-pipeline require explicit ``allow_pair_switch`` here, not just at
    execution, so mismatches fail fast with 400. Connectionless jobs
    (``need="none"``) skip snapshotting and gating. Resume callers pass
    ``allow_statuses`` including failed/cancelled. ``root_path`` prefixes
    the poll URL so subpath-mounted deployments get a working target.
    """
    from aap_migration.api.context import check_pair_switch

    source_id, target_id = _body_source_target(body)
    job_ref = _body_job_id(body)
    # Chaining validation against context.resolve_workdir (the single home
    # for chaining rules); errors map to HTTP codes so submit-time
    # validation matches execution.
    if job_ref:
        from aap_migration.api.context import resolve_workdir

        try:
            reuse = str(
                resolve_workdir(
                    {"job_id": job_ref},
                    get_job_manager().base_dir,
                    allow_statuses=allow_statuses,
                )
            )
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=_key_detail(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_detail(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            message = str(exc)
            if "working directory no longer exists" in message:
                raise HTTPException(status_code=404, detail=message) from exc
            raise HTTPException(status_code=409, detail=message) from exc
    else:
        reuse = None
    dumped = body.model_dump() if hasattr(body, "model_dump") else dict(body)
    if needs_connections(need):
        # Pin the effective pair with a single resolution (no double-read):
        # submit_pair_snapshot resolves the active pair once via
        # pair_fingerprint, so validation and pinning can never observe
        # different pairs across an admin set_active. A separate
        # require_connections call would re-resolve and reopen the race.
        # Fingerprint failures fail closed (400/404) so no job is ever
        # enqueued without snapshot keys.
        try:
            from aap_migration.api.context import submit_pair_snapshot

            dumped.update(submit_pair_snapshot(source_id, target_id, need))
        except (KeyError, ValueError, QueueFullError, InternalStatusError) as exc:
            raise _store_http_error(exc) from exc
    if job_ref and reuse and needs_connections(need):
        # Connectionless jobs (need="none", e.g. iam-report) skip this gate:
        # their schemas carry no selectors and no allow_pair_switch field,
        # so enforcing here would demand a flag callers cannot supply.
        try:
            referenced = get_job_manager().get_internal(job_ref)
        except KeyError:
            referenced = None
        if referenced is not None:
            try:
                check_pair_switch(
                    dumped,
                    referenced.get("params", {}),
                    snapshot={
                        k: v
                        for k, v in {
                            "source_id": dumped.get(SNAPSHOT_SOURCE_ID),
                            "target_id": dumped.get(SNAPSHOT_TARGET_ID),
                        }.items()
                        if v is not None
                    }
                    or None,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
    return submit_job(job_type, dumped, func, job_dir=reuse, root_path=root_path)
