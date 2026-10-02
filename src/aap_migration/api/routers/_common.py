"""Shared router helpers: error mapping, job submission, pre-validation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, cast

from fastapi import HTTPException

from aap_migration.api.jobs import (
    API_V1_PREFIX,
    ConflictError,
    JobRecord,
    QueueFullError,
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

    A future manager status (or a typo) surfaces here as a greppable
    ValueError (400) instead of an unchecked cast that blows up later as a
    response-validation 500.
    """
    from typing import get_args

    if value not in get_args(JobStatusValue):
        raise ValueError(f"Unknown job status '{value}'")
    return cast(JobStatusValue, value)


def submit_job(
    job_type: str,
    params: dict[str, Any],
    func: Callable[[JobRecord], dict[str, Any]],
    job_dir: str | None = None,
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
        poll_url=f"{API_V1_PREFIX}/jobs/{job['job_id']}",
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
    """Single home for store/manager error -> HTTP mapping (type-based)."""
    if isinstance(exc, UnknownJobError) or isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=_key_detail(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, QueueFullError):
        message = str(exc)
        if message.startswith(("Server storage unhealthy", "Server is shutting down")):
            return HTTPException(status_code=503, detail=message)
        return HTTPException(status_code=429, detail=message)
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc  # pragma: no cover


def handle_store_errors(func: Callable, *args: Any, **kwargs: Any) -> Any:
    """Map store-layer errors to HTTP errors (single home, type-based)."""
    try:
        return func(*args, **kwargs)
    except (KeyError, ValueError, QueueFullError) as exc:
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
    except (KeyError, ValueError, QueueFullError) as exc:
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
) -> JobCreated:
    """Pre-validate connections + chaining, then enqueue the job.

    Submit-time checks mirror execution-time resolution, and the effective
    pair is fingerprinted into stored params (``_snapshot_*``) so workers
    fail fast when connections drift between submit (202) and execution
    instead of silently switching credentials or targets. Pair switches
    mid-pipeline require explicit ``allow_pair_switch`` here, not just at
    execution, so mismatches fail fast with 400. Connectionless jobs
    (``need="none"``) skip snapshotting and gating. Resume callers pass
    ``allow_statuses`` including failed/cancelled.
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
        except (KeyError, ValueError, QueueFullError) as exc:
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
    return submit_job(job_type, dumped, func, job_dir=reuse)
