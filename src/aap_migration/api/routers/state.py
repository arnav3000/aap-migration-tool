"""State + retry endpoints (mirrors ``state`` and ``retry`` groups)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from aap_migration.api import services
from aap_migration.api.context import open_default_state
from aap_migration.api.jobs import TERMINAL_STATUSES, get_job_manager
from aap_migration.api.routers._common import _get_state, submit_chained, submit_job
from aap_migration.api.schemas import (
    CheckpointCreate,
    CheckpointCreateOut,
    CheckpointsListOut,
    DeleteCheckpointOut,
    JobCreated,
    MappingsListOut,
    ResumeInfoOut,
    RetryFailedRequest,
    RetryStatusOut,
    StateExportRequest,
    StateImportOut,
    StateImportRequest,
    StateResetOut,
    StateResetRequest,
    StateShowOut,
)

log = logging.getLogger("aap_migration.api.state")

router = APIRouter(tags=["state"])


def _reject_if_job_active(job_id: str | None) -> None:
    """Reject destructive state writes against a queued/running job (409).

    Delegates to :meth:`JobManager.assert_job_terminal` (single home with
    ``reset_state``): resetting or importing into a state DB while its
    owning job is still writing converts a resumable job into partial
    progress with no checkpoint.
    """
    if not job_id:
        return
    from aap_migration.api.jobs import get_job_manager
    from aap_migration.api.routers._common import _store_http_error

    try:
        get_job_manager().assert_job_terminal(job_id)
    except (KeyError, ValueError) as exc:
        raise _store_http_error(exc) from exc


def _reject_if_any_job_active() -> None:
    """Reject server-default full resets while any job is queued/running (409).

    Delegates to :meth:`JobManager.assert_no_active_jobs` (single home with
    ``reset_state``).
    """
    from aap_migration.api.jobs import manager_or_none
    from aap_migration.api.routers._common import _store_http_error

    manager = manager_or_none()
    if manager is None:
        return
    try:
        manager.assert_no_active_jobs()
    except (KeyError, ValueError) as exc:  # pragma: no cover - defensive parity
        raise _store_http_error(exc) from exc


@router.get("/state/show", response_model=StateShowOut, response_model_exclude_unset=True)
def show_state(
    detailed: bool = False,
    job_id: str | None = Query(
        default=None, description="When set, read the chained job's state DB"
    ),
    strict: bool = Query(
        default=False,
        description="When true, return 404 instead of 200+warning when no state DB exists",
    ),
) -> dict:
    """Show migration state (mirrors ``state show``).

    By default (CLI parity) a missing DB returns 200 with a ``warning`` key.
    Pass ``strict=true`` for a 404 ``{"detail": "No migration state DB found"}``
    instead.
    """
    state, missing = _get_state(
        job_id,
        strict,
        {"migration_id": None, "warning": "No migration state DB found", "stats": {}},
    )
    if missing is not None:
        return missing
    assert state is not None
    try:
        stats = state.get_migration_stats()
    except Exception as exc:
        log.exception("state show: migration stats unreadable")
        raise HTTPException(
            status_code=500, detail="State database unreadable (see server logs for detail)"
        ) from exc
    payload: dict = {
        "migration_id": state.migration_id,
        "stats": stats,
    }
    if detailed:
        try:
            payload["import_stats_by_type"] = {
                rtype: state.get_import_stats(rtype)
                for rtype in (
                    "organizations",
                    "inventories",
                    "hosts",
                    "projects",
                    "credentials",
                    "job_templates",
                )
            }
        except Exception as exc:
            log.exception("state show: detailed import stats unreadable")
            raise HTTPException(
                status_code=500,
                detail="State database unreadable (see server logs for detail)",
            ) from exc
    return payload


@router.get("/state/mappings", response_model=MappingsListOut, response_model_exclude_unset=True)
def show_mappings(
    resource_type: str | None = None,
    source_record_id: int | None = Query(
        default=None, description="Numeric source record id for a single mapping lookup"
    ),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=100000),
    job_id: str | None = Query(
        default=None, description="When set, read the chained job's state DB"
    ),
    strict: bool = Query(
        default=False,
        description="When true, return 404 instead of 200+warning when no state DB exists",
    ),
) -> dict:
    """Show source->target ID mappings (mirrors ``state mappings``).

    By default (CLI parity) a missing DB returns 200 with a ``warning`` key.
    Pass ``strict=true`` for a 404 ``{"detail": "No migration state DB found"}``
    instead.
    """
    state, missing = _get_state(
        job_id,
        strict,
        {
            "mappings": [],
            "limit": limit,
            "offset": offset,
            "total": 0,
            "warning": "No migration state DB found",
        },
    )
    if missing is not None:
        return missing
    assert state is not None
    if source_record_id is not None and resource_type:
        target_id = state.get_mapped_id(resource_type, source_record_id)
        # Single-lookup branch keeps the same paginated envelope as the
        # list branch (total 0/1) so typed clients reuse one shape.
        if target_id is None:
            return {"mappings": [], "limit": limit, "offset": offset, "total": 0}
        return {
            "mappings": [
                {
                    "resource_type": resource_type,
                    "source_id": source_record_id,
                    "target_id": target_id,
                }
            ],
            "limit": limit,
            "offset": offset,
            "total": 1,
        }
    from aap_migration.migration.database import get_session
    from aap_migration.migration.models import IDMapping

    with get_session(state.database_url) as session:
        query = session.query(IDMapping).order_by(IDMapping.id)
        if resource_type:
            query = query.filter(IDMapping.resource_type == resource_type)
        from sqlalchemy import func as _func

        total = int(
            session.query(_func.count(IDMapping.id))
            .filter(*([IDMapping.resource_type == resource_type] if resource_type else []))
            .scalar()
            or 0
        )
        rows = query.offset(offset).limit(limit).all()
        return {
            "mappings": [
                {
                    "resource_type": r.resource_type,
                    "source_id": r.source_id,
                    "target_id": r.target_id,
                    "source_name": getattr(r, "source_name", None),
                    "target_name": getattr(r, "target_name", None),
                }
                for r in rows
            ],
            "limit": limit,
            "offset": offset,
            "total": total,
        }


def _apply_reset(state: object, resource_type: str | None, keep_mappings: bool) -> dict:
    """Apply a state reset and build the single response envelope.

    Single home for the three reset envelopes (resource_type /
    keep_mappings / full reset_database+init_database) used by job-scoped,
    server-default, and post-lock fallback paths in ``reset_state``.
    Lock/guard orchestration stays in ``reset_state``; this helper owns
    only mutation + envelope so the three sites cannot drift.
    """
    if resource_type:
        cleared = state.clear_progress(resource_type)  # type: ignore[attr-defined]
        reset = 0
        if not keep_mappings:
            reset = state.reset_target_ids(resource_type)  # type: ignore[attr-defined]
        return {
            "resource_type": resource_type,
            "cleared_progress": cleared,
            "reset_mappings": reset,
            "reset": None,
            "keep_mappings": keep_mappings,
        }
    if keep_mappings:
        from aap_migration.resources import ALL_RESOURCE_TYPES

        total = sum(state.clear_progress(rt) for rt in ALL_RESOURCE_TYPES)  # type: ignore[attr-defined]
        return {
            "resource_type": None,
            "cleared_progress": total,
            "reset_mappings": 0,
            "reset": None,
            "keep_mappings": True,
        }
    from aap_migration.migration.database import init_database, reset_database

    reset_database(state.database_url)  # type: ignore[attr-defined]
    init_database(state.database_url)  # type: ignore[attr-defined]
    return {
        "resource_type": None,
        "cleared_progress": 0,
        "reset_mappings": 0,
        "reset": "all",
        "keep_mappings": False,
    }


@router.post("/state/reset", response_model=StateResetOut)
def reset_state(body: StateResetRequest) -> dict:
    """Reset migration state (mirrors ``state reset``).

    Without ``job_id`` resets the server-default state; with ``job_id``
    resets that job's isolated ``migration_state.db`` (404 for unknown jobs
    or jobs with no state DB yet). Full resets (``reset_database`` path)
    require zero queued/running jobs (409 otherwise); job-scoped resets
    require the referenced job to be terminal (409 otherwise).

    One envelope always: ``{"resource_type", "cleared_progress",
    "reset_mappings", "reset", "keep_mappings"}``. Counts stay numeric on
    every branch (the full-reset branch drops the DB instead of counting
    rows, so its counts are 0 and ``reset`` is ``"all"`` as the
    discriminator).

    The active-job guard and the destructive write run atomically under
    the job-manager lock (guard re-checked while held): a job submitted
    between the check and the write cannot interleave progress rows with
    the reset. Submits block briefly instead of racing; the worker never
    needs this lock to finish a phase, so no deadlock.
    """

    from aap_migration.api.jobs import manager_or_none

    full_reset = not body.resource_type and not body.keep_mappings
    global_write = full_reset and body.job_id is None
    manager = manager_or_none()
    if manager is None:
        if global_write:
            _reject_if_any_job_active()
        else:
            _reject_if_job_active(body.job_id)
        if not body.job_id:
            state = open_default_state()
        else:
            from aap_migration.api.context import resolve_job_state

            _, state = resolve_job_state(body.job_id, strict=True)
        if state is None:
            raise HTTPException(status_code=404, detail="No migration state DB found")
    else:
        # Guards live in JobManager and the destructive write runs under
        # state_write_lock (guard re-checked while held): submits block
        # briefly instead of racing. Fences (live orphans) also 409.
        from aap_migration.api.routers._common import _store_http_error

        try:
            with manager.state_write_lock():
                manager.assert_no_live_fences()
                if global_write:
                    manager.assert_no_active_jobs_locked()
                elif body.job_id:
                    manager.assert_job_terminal_locked(body.job_id)
                elif not body.job_id:
                    # Any server-default write without job_id (full or
                    # partial reset via resource_type/keep_mappings)
                    # bulk-writes the shared DB: guard like a full reset.
                    manager.assert_no_active_jobs_locked()
                if body.job_id:
                    from aap_migration.api.context import resolve_job_state

                    _, state = resolve_job_state(body.job_id, strict=True)
                    if state is None:
                        raise HTTPException(status_code=404, detail="No migration state DB found")
                    # Job-scoped writes run inside the lock (atomic with
                    # the terminal guard above): returning here keeps the
                    # guard and bulk write in one critical section.
                    return _apply_reset(state, body.resource_type, body.keep_mappings)
                else:
                    state = open_default_state()
                    if state is None:
                        raise HTTPException(status_code=404, detail="No migration state DB found")
                    return _apply_reset(state, body.resource_type, body.keep_mappings)
        except (KeyError, ValueError) as exc:
            # Single home for the mapping (P2 #20): UnknownJobError/KeyError
            # (incl. WorkdirGoneError) -> 404, ConflictError -> 409, any
            # other ValueError -> 400.
            raise _store_http_error(exc) from exc
        if body.job_id:
            from aap_migration.api.context import resolve_job_state as _resolve_job_state

            _, state = _resolve_job_state(body.job_id, strict=True)
            if state is None:
                raise HTTPException(status_code=404, detail="No migration state DB found")
        else:
            # Unreachable: server-default writes returned inside the lock.
            state = open_default_state()
            if state is None:
                raise HTTPException(status_code=404, detail="No migration state DB found")
    return _apply_reset(state, body.resource_type, body.keep_mappings)


@router.post("/state/export", response_model=JobCreated, status_code=202)
def export_state(body: StateExportRequest, request: Request) -> JobCreated:
    """Export migration state to a JSON backup file (mirrors ``state export``)."""
    return submit_job(
        "state-export",
        body.model_dump(),
        services.run_state_export,
        root_path=request.scope.get("root_path", ""),
    )


@router.post("/state/import", response_model=StateImportOut)
def import_state(body: StateImportRequest) -> dict:
    """Import migration state from a JSON backup confined to the job tree.

    Job-scoped reads are confined to the referenced job's directory: pass
    ``job_id`` to import a file produced by that job into that job's
    isolated ``migration_state.db`` (404 for unknown jobs or jobs with no
    state DB yet). Without ``job_id``, files inside per-job directories are
    rejected (import them with the owning ``job_id`` instead) and the import
    targets the server-default DB, which requires zero queued/running jobs
    (409 otherwise), mirroring the full-reset guard: a bulk import landing
    mid-retry would interleave rows with the worker's progress writes.
    Job-scoped imports additionally require the referenced job to be
    terminal (409 otherwise) plus the global no-active-jobs guard, since a
    bulk write racing any live worker risks the same interleaving.

    The active-job guard and the import run atomically under the
    job-manager lock (guard re-checked while held), mirroring
    ``reset_state``: submits block briefly instead of racing the bulk
    write; the worker never needs this lock to finish a phase.

    Returns the write scope (``job`` vs ``server-default``) alongside the
    imported filename so callers can detect which DB received the rows.
    """
    import os

    from aap_migration.api.security import confine_path

    if body.job_id:
        try:
            ref = get_job_manager().get_internal(body.job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown job_id '{body.job_id}'") from exc
        base = ref["job_dir"]
    else:
        base = get_job_manager().base_dir
    try:
        confined = confine_path(body.state_file, base, label="state_file")
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="state_file must stay under the job file tree"
        ) from exc
    if not body.job_id:
        try:
            rel = os.path.relpath(str(confined), os.path.abspath(base))
            if rel != "." and os.sep in rel:
                raise HTTPException(
                    status_code=400,
                    detail="state_file inside a job directory requires job_id",
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="state_file must stay under the job file tree"
            ) from exc
    if not confined.is_file():
        raise HTTPException(status_code=404, detail="State backup file not found")
    # Bound the read before json.loads: parse amplifies memory several
    # times file size on a sync thread (CWE-400). Legitimate backups fit
    # well under the cap; override via AAP_BRIDGE_MAX_STATE_IMPORT_BYTES.
    try:
        max_bytes = int(os.environ.get("AAP_BRIDGE_MAX_STATE_IMPORT_BYTES", str(64 << 20)))
    except ValueError:
        max_bytes = 64 << 20
    try:
        if confined.stat().st_size > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"State backup exceeds {max_bytes} bytes; split or raise AAP_BRIDGE_MAX_STATE_IMPORT_BYTES",
            )
    except OSError as exc:
        raise HTTPException(status_code=404, detail="State backup file not found") from exc
    try:
        from aap_migration.api.jobs import manager_or_none as _manager_or_none

        _manager = _manager_or_none()
        if _manager is None:
            if body.job_id:
                _reject_if_job_active(body.job_id)
                # The bulk write targets the referenced job's DB, so an
                # unrelated live job must also block (global guard).
                _reject_if_any_job_active()
                from aap_migration.api.context import resolve_job_state

                _, state = resolve_job_state(body.job_id, strict=True)
                scope = "job"
            else:
                _reject_if_any_job_active()
                state = open_default_state()
                scope = "server-default"
            if state is None:
                raise HTTPException(status_code=404, detail="No migration state DB found")
            state.import_state(str(confined))
        else:
            from aap_migration.api.routers._common import _store_http_error as _she

            try:
                with _manager.state_write_lock():
                    _manager.assert_no_live_fences()
                    if body.job_id:
                        _manager.assert_job_terminal_locked(body.job_id)
                        # The bulk write targets the referenced job's DB, so
                        # an unrelated live job must also block (global guard).
                        _manager.assert_no_active_jobs_locked()
                        from aap_migration.api.context import (
                            resolve_job_state as _resolve_job_state,
                        )

                        _, state = _resolve_job_state(body.job_id, strict=True)
                        scope = "job"
                    else:
                        _manager.assert_no_active_jobs_locked()
                        state = open_default_state()
                        scope = "server-default"
                    if state is None:
                        raise HTTPException(status_code=404, detail="No migration state DB found")
                    state.import_state(str(confined))
            except (KeyError, ValueError) as exc:
                raise _she(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("state import failed")
        raise HTTPException(
            status_code=500, detail="Import failed (see server logs for detail)"
        ) from exc
    try:
        shown = str(confined.relative_to(os.path.abspath(base)))
    except ValueError:
        shown = confined.name
    return {"imported": shown, "scope": scope}


@router.post("/retry/failed", response_model=JobCreated, status_code=202)
def retry_failed(body: RetryFailedRequest, request: Request) -> JobCreated:
    """Retry failed imports (mirrors ``retry failed``)."""
    return submit_chained(
        "retry-failed",
        body,
        services.run_retry_failed,
        need="both",
        allow_statuses=TERMINAL_STATUSES,
        root_path=request.scope.get("root_path", ""),
    )


@router.get("/retry/status", response_model=RetryStatusOut, response_model_exclude_unset=True)
def retry_status(
    resource_types: list[str] | None = Query(default=None),
    job_id: str | None = Query(
        default=None, description="When set, read the chained job's state DB"
    ),
    strict: bool = Query(
        default=False,
        description="When true, return 404 instead of 200+warning when no state DB exists",
    ),
) -> dict:
    """Pending/failed/completed counts (mirrors ``retry status``).

    By default (CLI parity) a missing DB returns 200 with a ``warning`` key.
    Pass ``strict=true`` for a 404 ``{"detail": "No migration state DB found"}``
    instead.
    """
    from sqlalchemy import func

    state, missing = _get_state(
        job_id, strict, {"by_type": {}, "warning": "No migration state DB found"}
    )
    if missing is not None:
        return missing
    assert state is not None
    from aap_migration.migration.database import get_session
    from aap_migration.migration.models import MigrationProgress

    # Accept both repeated ?resource_types=a&resource_types=b and legacy
    # comma-separated ?resource_types=a,b.
    requested: list[str] = []
    for entry in resource_types or []:
        requested.extend(r.strip() for r in entry.split(",") if r.strip())
    with get_session(state.database_url) as session:
        query = session.query(
            MigrationProgress.resource_type,
            MigrationProgress.status,
            func.count(MigrationProgress.id),
        )
        if requested:
            query = query.filter(MigrationProgress.resource_type.in_(requested))
        rows = (
            query.group_by(MigrationProgress.resource_type, MigrationProgress.status)
            .order_by(MigrationProgress.resource_type, MigrationProgress.status)
            .all()
        )
    by_type: dict = {}
    for rtype, status, count in rows:
        by_type.setdefault(rtype, {}).setdefault(status or "pending", 0)
        by_type[rtype][status or "pending"] += count
    return {"by_type": by_type}


@router.get("/checkpoints", response_model=CheckpointsListOut, response_model_exclude_unset=True)
def list_checkpoints(
    phase: str | None = None,
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=100000),
    job_id: str | None = Query(
        default=None, description="When set, read the chained job's state DB"
    ),
    strict: bool = Query(
        default=False,
        description="When true, return 404 instead of 200+warning when no state DB exists",
    ),
    migration_id: str | None = Query(
        default=None,
        description="Filter by migration ID (omit for every migration's "
        "checkpoints). Each API request opens a fresh state with a new ID, "
        "so the current-ID default would always be empty. Do not pass "
        "'all' as an ID: it collides with the list-all sentinel by design; "
        "use UUIDs.",
    ),
) -> dict:
    """List migration checkpoints.

    By default (CLI parity) a missing DB returns 200 with a ``warning`` key.
    Pass ``strict=true`` for a 404 ``{"detail": "No migration state DB found"}``
    instead. Omit ``migration_id`` to list every migration's checkpoints;
    a literal ID ``all`` is reserved for the list-all sentinel and never
    filters (migration IDs are UUIDs).
    """
    from aap_migration.migration.checkpoint import CheckpointManager

    # Omitted means every migration's checkpoints. The explicit 'all'
    # alias is removed: only omission lists all, and a literal ID 'all'
    # collides with the manager's list-all sentinel by design (unreachable
    # as a filter -- prefer UUIDs, see manager). Kept backward-compat:
    # a literal 'all' still lists all rather than filtering empty, but
    # clients must omit instead of passing it.
    migration_filter = "all" if migration_id is None else migration_id
    state, missing = _get_state(
        job_id,
        strict,
        {
            "checkpoints": [],
            "limit": limit,
            "offset": offset,
            "total": 0,
            "warning": "No migration state DB found",
        },
    )
    if missing is not None:
        return missing
    assert state is not None
    manager = CheckpointManager(state)
    total = manager.count_checkpoints(phase=phase, migration_id=migration_filter)
    checkpoints = manager.list_checkpoints(
        phase=phase, limit=limit, offset=offset, migration_id=migration_filter
    )
    return {"checkpoints": checkpoints, "limit": limit, "offset": offset, "total": total}


@router.get(
    "/checkpoints/resume-info",
    response_model=ResumeInfoOut,
    response_model_exclude_unset=True,
)
def checkpoint_resume_info(
    job_id: str | None = Query(
        default=None, description="When set, read the chained job's state DB"
    ),
    strict: bool = Query(
        default=False,
        description="When true, return 404 instead of 200+warning when no state DB exists",
    ),
) -> dict:
    """Where a resumed migration would continue from.

    Always returns ``{"resumable": bool, "resume_from": ...}`` with
    ``resumable: false`` and ``resume_from: null`` when no resume point
    exists (never a bare ``{}``), so typed clients never branch on key
    presence. By default a missing DB returns 200 with the same stable
    key set plus a ``warning`` key; pass ``strict=true`` for a 404 instead.
    """
    state, missing = _get_state(
        job_id,
        strict,
        {
            "resumable": False,
            "resume_from": None,
            "warning": "No migration state DB found",
        },
    )
    if missing is not None:
        return missing
    assert state is not None
    from aap_migration.migration.checkpoint import CheckpointManager

    info = CheckpointManager(state).get_resume_info()
    if not info:
        return {"resumable": False, "resume_from": None}
    return {"resumable": True, "resume_from": info}


@router.post("/checkpoints", status_code=201, response_model=CheckpointCreateOut)
def create_checkpoint(body: CheckpointCreate) -> dict:
    """Create a checkpoint for the current progress.

    Without ``job_id`` targets the server-default state; with ``job_id``
    targets that job's isolated ``migration_state.db`` (404 for unknown jobs
    or jobs with no state DB yet). Returns the integer ``checkpoint_id``
    plus the ``migration_id`` it was stored under (pass it back to
    ``GET /checkpoints?migration_id=`` to scope the listing).
    """
    from aap_migration.migration.checkpoint import CheckpointManager

    if not body.job_id:
        state = open_default_state()
    else:
        from aap_migration.api.context import resolve_job_state

        _, state = resolve_job_state(body.job_id, strict=True)
    if state is None:
        raise HTTPException(status_code=404, detail="No migration state DB found")
    try:
        checkpoint_id = CheckpointManager(state).create_checkpoint(
            phase=body.phase,
            progress_stats=body.progress_stats,
            checkpoint_data=body.checkpoint_data,
            description=body.description,
            checkpoint_name=body.name,
        )
    except Exception as exc:
        log.exception("checkpoint creation failed")
        raise HTTPException(
            status_code=500, detail="Checkpoint creation failed (see server logs for detail)"
        ) from exc
    return {"checkpoint_id": checkpoint_id, "migration_id": state.migration_id}


@router.delete("/checkpoints/{checkpoint_id}", response_model=DeleteCheckpointOut)
def delete_checkpoint(
    checkpoint_id: int,
    job_id: str | None = Query(
        default=None, description="When set, delete from the chained job's state DB"
    ),
) -> dict:
    """Delete a checkpoint.

    Without ``job_id`` targets the server-default state; with ``job_id``
    targets that job's isolated ``migration_state.db`` (404 for unknown jobs
    or jobs with no state DB yet).
    """
    from aap_migration.client.exceptions import CheckpointError
    from aap_migration.migration.checkpoint import CheckpointManager

    if not job_id:
        state = open_default_state()
    else:
        from aap_migration.api.context import resolve_job_state

        _, state = resolve_job_state(job_id, strict=True)
    if state is None:
        raise HTTPException(status_code=404, detail="No migration state DB found")
    try:
        CheckpointManager(state).delete_checkpoint(checkpoint_id)
    except CheckpointError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Create returns an integer id, so delete echoes the same integer under
    # a type-distinct key (P2 #13: one deleted key must not carry both str
    # and int across endpoints).
    return {"deleted_checkpoint_id": checkpoint_id}
