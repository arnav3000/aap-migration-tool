"""Shared router helpers: error mapping, job submission, pre-validation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypeVar, cast

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

# Single home lives in api._errors (neutral, depends on nothing internal):
# routers import the helpers here, never through underscore-private
# jobs._records (which only re-exports them for backward compatibility).
from aap_migration.api._errors import _key_detail as _key_detail
from aap_migration.api._errors import _store_http_error as _store_http_error
from aap_migration.api._paths import API_V1_PREFIX
from aap_migration.api.jobs import (
    InternalStatusError,
    JobRecord,
    QueueFullError,
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
    SNAPSHOT_FP,
    SNAPSHOT_NEED,
    SNAPSHOT_SOURCE_ID,
    SNAPSHOT_TARGET_ID,
    ConnectionScope,
    NeedScope,
    needs_connections,
)

_T = TypeVar("_T")


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


def _poll_url(job_id: str, root_path: str = "") -> str:
    """Build a root_path-aware poll URL for subpath-mounted deployments."""
    prefix = (root_path or "").rstrip("/")
    return f"{prefix}{API_V1_PREFIX}/jobs/{job_id}"


def _require_submit_pins(params: dict[str, Any]) -> None:
    """Fail fast when connection-bearing params carry no snapshot pins.

    Mirrors the execution-side pin assert in
    ``services._core.chained_ctx`` (which raises KeyError) at submit time
    with a 400: connection selectors without ``_snapshot_*`` pins mean the
    caller bypassed :func:`submit_chained`, so no drift guard exists for
    the run. Connectionless params (no selectors, ``need="none"`` pins, or
    complete pins) pass through.
    """
    need = params.get(SNAPSHOT_NEED)
    if need is not None and need != "none":
        for _key in (
            SNAPSHOT_SOURCE_ID,
            SNAPSHOT_TARGET_ID,
            SNAPSHOT_FP,
            SNAPSHOT_NEED,
        ):
            if _key not in params:
                raise ValueError(
                    f"job params declare {SNAPSHOT_NEED}={need!r} but miss "
                    f"required pin key {_key!r}; submit connection-bearing "
                    "jobs via submit_chained"
                )
        return
    if need is None and ("source_id" in params or "target_id" in params):
        # Note: "job_id" alone is not a trigger -- connectionless chained
        # submits (need="none", e.g. iam-report re-rendering onto a
        # referenced workdir) legitimately carry job_id without pins.
        raise ValueError(
            "connection-bearing params without snapshot pins; submit via "
            "submit_chained so the effective pair is fingerprinted"
        )


def submit_job(
    job_type: str,
    params: dict[str, Any],
    func: Callable[[JobRecord], dict[str, Any]],
    job_dir: str | None = None,
    root_path: str = "",
) -> JobCreated:
    """Enqueue a background job and build the response model.

    Low-level enqueue: connection-bearing callers must use
    :func:`submit_chained` instead so the effective pair is fingerprinted
    into stored params and re-verified at execution. Direct callers pass
    connectionless params (or complete ``_snapshot_*`` pins); anything
    else fails fast here with 400 instead of waiting out the FIFO queue
    and failing at execution.
    """
    _require_submit_pins(params)
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


def handle_store_errors(  # noqa: UP047 - TypeVar keeps mypy (no PEP 695) green
    func: Callable[..., _T], *args: Any, **kwargs: Any
) -> _T:
    """Map store-layer errors to HTTP errors (single home, type-based)."""
    try:
        return func(*args, **kwargs)
    except (KeyError, ValueError, IntegrityError, QueueFullError, InternalStatusError) as exc:
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
        except (KeyError, ValueError) as exc:
            # Single home for the mapping (P2 #20): UnknownJobError/KeyError
            # (incl. WorkdirGoneError) -> 404, ConflictError -> 409, any
            # other ValueError -> 400. Codes come from exception types
            # only, never message text.
            raise _store_http_error(exc) from exc
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

            dumped.update(
                submit_pair_snapshot(
                    ConnectionScope(
                        source_id=source_id, target_id=target_id, need=cast(NeedScope, need)
                    )
                )
            )
        except (KeyError, ValueError, QueueFullError, InternalStatusError) as exc:
            raise _store_http_error(exc) from exc
        # TLS posture veto (P1 #3): a per-job verify_ssl=false against a
        # stored verify_ssl=true would run weaker TLS than the snapshot
        # attests -- fail closed with 400 instead of enqueueing.
        try:
            from aap_migration.api.context import check_tls_posture

            check_tls_posture(dumped, need=cast(NeedScope, need))
        except (KeyError, ValueError) as exc:
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
    created = submit_job(job_type, dumped, func, job_dir=reuse, root_path=root_path)
    if needs_connections(need):
        _reverify_post_submit(source_id, target_id, need, dumped, created.job_id)
    return created


def _reverify_post_submit(
    source_id: str | None,
    target_id: str | None,
    need: Literal["both", "source", "none"],
    dumped: dict[str, Any],
    job_id: str,
) -> None:
    """Fail-fast re-verification immediately after enqueue (P1 #4).

    An admin set_active that commits between the submit-time fingerprint
    and the enqueue is invisible to the move veto (the job was not yet
    enqueued, so the veto's pending-refs snapshot cannot see it): without
    this recheck the job sits accepted and is doomed to fail at execution
    after a full queue wait. Re-resolve immediately: on drift, fail the
    just-enqueued job with an actionable message and raise 409 so the
    client resubmits under the current pair. The residual micro-window (a
    move landing after this recheck) is still caught at execution by
    ``verify_execution_pair``. Raises HTTPException on drift.
    """
    from aap_migration.api.store import pair_fingerprint

    try:
        current = pair_fingerprint(
            ConnectionScope(source_id=source_id, target_id=target_id, need=cast(NeedScope, need))
        )
    except (KeyError, ValueError) as exc:
        # fail_fast returns False when the job already left queued (worker
        # dequeued between enqueue and this recheck): the job is running
        # and doomed to fail at execution. Return with the job_id intact
        # so the client can poll it; execution-time verify fails it there.
        # Only raise when fail_fast actually failed the queued job.
        if get_job_manager().fail_fast(
            job_id,
            f"Connections changed while this job was submitted ({exc}); "
            "resubmit to run under the current pair.",
        ):
            raise _store_http_error(exc) from exc
        return
    snap_src = dumped.get(SNAPSHOT_SOURCE_ID)
    snap_tgt = dumped.get(SNAPSHOT_TARGET_ID)
    snap_fp = dumped.get(SNAPSHOT_FP)
    allow_switch = bool(dumped.get("allow_pair_switch", False))
    ids_changed = (snap_src is not None and current["source_id"] != snap_src) or (
        snap_tgt is not None and current["target_id"] != snap_tgt
    )
    fp_changed = snap_fp is not None and current["fp"] != snap_fp
    # Same predicate as verify_execution_pair: opt-in covers id switches
    # with an unchanged fingerprint only; any fingerprint drift fails.
    doomed = fp_changed or (ids_changed and not allow_switch)
    if doomed:
        message = (
            "Connections changed while this job was submitted "
            f"(snapshot {snap_src}/{snap_tgt} vs current "
            f"{current['source_id']}/{current['target_id']}); the queued "
            "job was failed immediately -- resubmit to run under the "
            "current pair."
        )
        if get_job_manager().fail_fast(job_id, message):
            raise HTTPException(status_code=409, detail=message)
        return
