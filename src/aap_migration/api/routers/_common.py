"""Shared router helpers: error mapping, job submission, pre-validation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypeVar, cast

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from aap_migration.api._paths import API_V1_PREFIX
from aap_migration.api.jobs import (
    InternalStatusError,
    JobRecord,
    QueueFullError,
    get_job_manager,
)

# Re-exported (single home lives in foundation jobs._records): routers
# depend on foundation, never the reverse (stack 3 review #1).
from aap_migration.api.jobs._records import _key_detail as _key_detail
from aap_migration.api.jobs._records import _store_http_error as _store_http_error
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
    SNAPSHOT_SOURCE_ID,
    SNAPSHOT_TARGET_ID,
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


def _get_state(
    job_id: str | None, strict: bool, empty: dict[str, Any]
) -> tuple[Any | None, dict[str, Any] | None]:
    """Shared missing-DB branch for state readers (single home).

    Returns ``(state, None)`` when a state DB resolves, otherwise
    ``(None, empty)`` for the lenient CLI-parity shape, raising 404 when
    ``strict`` opts into the missing-DB-as-error contract.
    """
    from aap_migration.api.context import open_default_state

    if not job_id:
        state = open_default_state()
    else:
        from aap_migration.api.context import resolve_job_state

        _, state = resolve_job_state(job_id, strict=strict)
    if state is None:
        if strict:
            raise HTTPException(status_code=404, detail="No migration state DB found")
        return None, empty
    return state, None


def resolve_chained_scope(
    job_id: str,
    source_id: str | None,
    target_id: str | None,
    *,
    require_xformed: bool,
) -> tuple[Any, Any, str]:
    """Resolve a job-scoped dependency-read preamble (single home).

    Shared by ``POST /imports/check-dependencies`` and
    ``POST /validations/dependencies`` so the two readers cannot drift:
    resolve_job_state with the same 404/409 semantics, validate
    explicitly-passed connection ids only (targetless reads discard the
    built context, so source-only deployments without explicit ids get
    200/404 from chained state, not 400), then gate on the chained tree.

    With ``require_xformed=True`` (validation route) both the xformed
    directory and the chained DB must exist (404 otherwise); with False
    (migrations route) only the chained DB is required. Returns
    ``(workdir, chained_state, input_dir)`` where ``input_dir`` is the
    chained ``xformed`` dir as a string.
    """
    from pathlib import Path  # noqa: F401 -- re-exported for callers

    from aap_migration.api.context import build_ephemeral_context, resolve_job_state

    workdir, chained_state = resolve_job_state(job_id, strict=False, allow_statuses=("succeeded",))
    assert workdir is not None  # job_id is truthy, so a dir is returned
    if source_id or target_id:
        try:
            ctx = build_ephemeral_context(source_id, target_id)
            # Point at the chained tree so we never judge the ephemeral
            # server-default transform_dir (validation discards ctx;
            # migrations keeps this assignment for parity).
            ctx.config.paths.transform_dir = str(workdir / "xformed")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if require_xformed:
        chained = workdir / "xformed"
        if not chained.is_dir():
            raise HTTPException(
                status_code=404,
                detail=f"No transformed data yet for job '{job_id}'",
            )
        if chained_state is None:
            raise HTTPException(
                status_code=404,
                detail=f"No transformed data yet for job '{job_id}'",
            )
        return workdir, chained_state, str(chained)
    if chained_state is None:
        raise HTTPException(
            status_code=404,
            detail=f"No transformed data yet for job '{job_id}'",
        )
    return workdir, chained_state, str(workdir / "xformed")


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

            dumped.update(submit_pair_snapshot(source_id, target_id, need))
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
        current = pair_fingerprint(source_id, target_id, need=cast(NeedScope, need))
    except (KeyError, ValueError) as exc:
        get_job_manager().fail_fast(
            job_id,
            f"Connections changed while this job was submitted ({exc}); "
            "resubmit to run under the current pair.",
        )
        raise _store_http_error(exc) from exc
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
