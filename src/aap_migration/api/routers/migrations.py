"""Migration ETL endpoints.

Mirrors: ``migrate`` (full workflow + ``status`` + ``resume``), ``export``,
``transform``, ``import``, ``patch-projects``, and the granular import menu.
Long operations run as background jobs; status/statistics are synchronous.
"""

from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, HTTPException, Query

from aap_migration.api import services
from aap_migration.api.context import build_ephemeral_context
from aap_migration.api.jobs import TERMINAL_STATUSES
from aap_migration.api.routers._common import submit_chained
from aap_migration.api.schemas import (
    ExportRequest,
    GranularImportRequest,
    ImportDependencyCheckRequest,
    ImportRequest,
    JobCreated,
    MigrateRequest,
    MigrateResumeRequest,
    MigrationStatusOut,
    PatchProjectsRequest,
    PayloadCheckRequest,
    TransformRequest,
)

router = APIRouter(tags=["migrations"])


@router.post("/migrations", response_model=JobCreated, status_code=202)
def start_migration(body: MigrateRequest) -> JobCreated:
    """Run the full workflow: prep -> export -> transform -> import."""
    return submit_chained("migrate", body, services.run_migrate)


@router.get("/migrations/status", response_model=MigrationStatusOut)
def migration_status(
    source_id: str | None = None,
    target_id: str | None = None,
    job_id: str | None = Query(
        default=None, description="When set, read the chained job's state DB"
    ),
    strict: bool = Query(
        default=False,
        description="When true, return 404 instead of 200+warning when no state DB exists",
    ),
) -> dict:
    """Show migration status (mirrors ``migrate status``).

    By default (CLI parity) a missing DB returns 200 with a ``warning`` key,
    matching the state/mappings/retry/checkpoint readers. Pass
    ``strict=true`` for a 404 ``{"detail": "No migration state DB found"}``
    instead.

    Always HTTP 200 when a DB resolves. Each ``resource_stats`` value is
    the stats payload or null when that type is unreadable; unreadable
    types are listed in the top-level ``"unreadable_types": [...]`` (never
    as ``{"error": ...}`` sentinels inside the values).
    """
    from aap_migration.resources import ALL_RESOURCE_TYPES

    state = None
    if job_id:
        from aap_migration.api.context import resolve_job_state

        _, state = resolve_job_state(job_id, strict=strict)
        if state is None:
            # Known job with no state DB yet (queued / early running):
            # same 200+warning shape as the default scope so pollers use
            # one missing-DB rule until the worker creates the DB.
            # Unknown job_ids still 404 above (via the shared helper).
            return {
                "migration_id": None,
                "resource_stats": {},
                "unreadable_types": [],
                "warning": "No migration state DB found",
            }
    if state is None:
        # Server-default scope (CLI parity): read the same DB file the
        # state/* endpoints read (MIGRATION_STATE_DB_PATH aware) instead
        # of the ephemeral context default, so custom-path deployments
        # report the same stats everywhere and a GET never creates a DB.
        from aap_migration.api.context import default_state_db_path, open_state
        from aap_migration.api.routers._common import require_connections

        default_path = default_state_db_path()
        if default_path is None:
            # No state DB yet: still validate the pair so callers get the
            # same 400/404 connection errors as before, then report the
            # shared 200+warning missing-DB shape (never create a DB as a
            # GET side effect).
            require_connections(source_id, target_id)
            if strict:
                raise HTTPException(status_code=404, detail="No migration state DB found")
            return {
                "migration_id": None,
                "resource_stats": {},
                "unreadable_types": [],
                "warning": "No migration state DB found",
            }
        else:
            state = open_state(default_path)
    if state is None:
        if strict:
            raise HTTPException(status_code=404, detail="No migration state DB found")
        return {
            "migration_id": None,
            "resource_stats": {},
            "unreadable_types": [],
            "warning": "No migration state DB found",
        }

    stats: dict = {}
    unreadable_types: list = []
    for resource_type in ALL_RESOURCE_TYPES:
        try:
            stats[resource_type] = state.get_import_stats(resource_type)
        except Exception:
            stats[resource_type] = None
            unreadable_types.append(resource_type)
    return {
        "migration_id": state.migration_id,
        "resource_stats": stats,
        "unreadable_types": unreadable_types,
    }


@router.post("/migrations/resume", response_model=JobCreated, status_code=202)
def resume_migration(body: MigrateResumeRequest) -> JobCreated:
    """Resume from the last checkpoint (mirrors ``migrate resume``).

    Unlike other ETL phases, resume chains onto ``failed`` and ``cancelled``
    jobs (the ones that need resuming) as well as ``succeeded`` ones; only
    ``queued``/``running`` references are rejected with 409. The
    ``from_phase`` typo check returns 422 (request validation) with the
    CLI-parity message and normalized spelling.
    """
    from aap_migration.resources import ALL_RESOURCE_TYPES

    # CLI parity: --from-phase accepts any case (case_sensitive=False), so
    # normalize at the boundary and forward the canonical spelling; the
    # worker's MIGRATION_PHASES.index() is exact-case.
    canonical = None
    if body.from_phase:
        lowered = {str(p).lower(): str(p) for p in ALL_RESOURCE_TYPES}
        canonical = lowered.get(str(body.from_phase).lower())
        if canonical is None:
            raise HTTPException(status_code=422, detail=f"Unknown phase '{body.from_phase}'")
        body = body.model_copy(update={"from_phase": canonical})
    return submit_chained(
        "migrate-resume",
        body,
        services.run_migrate_resume,
        allow_statuses=TERMINAL_STATUSES,
    )


@router.post("/exports", response_model=JobCreated, status_code=202)
def start_export(body: ExportRequest) -> JobCreated:
    """Export RAW resources from source AAP (mirrors ``export``)."""
    return submit_chained("export", body, services.run_export)


@router.post("/transforms", response_model=JobCreated, status_code=202)
def start_transform(body: TransformRequest) -> JobCreated:
    """Transform RAW exports (mirrors ``transform``)."""
    return submit_chained("transform", body, services.run_transform)


@router.post("/transforms/preview")
def preview_transform(body: PayloadCheckRequest) -> dict:
    """Transform a single resource without persisting state (dry preview).

    Uses an isolated throwaway state DB so the preview has no side effects.
    One envelope always: ``{"resource_type", "valid", "errors",
    "transformed", "preview"}`` -- ``transformed`` is the converted payload
    on success and null on failure (a validation *result*, matching
    ``POST /validations/payload``), so callers branch on ``valid`` instead
    of key presence.
    """
    from aap_migration.config import StateConfig
    from aap_migration.migration.state import MigrationState
    from aap_migration.migration.transformer import SkipResourceError, create_transformer
    from aap_migration.resources import normalize_resource_type

    resource_type = normalize_resource_type(body.resource_type)

    tmp_dir = tempfile.mkdtemp(prefix="aap-preview-")
    try:
        preview_state = MigrationState(
            config=StateConfig(db_path=os.path.join(tmp_dir, "preview.db"))
        )
        try:
            transformer = create_transformer(resource_type, state=preview_state)
        except NotImplementedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            transformed = transformer.transform_resource(resource_type, dict(body.payload))
        except SkipResourceError as exc:
            missing = getattr(exc, "missing_dependency", None)
            return {
                "resource_type": resource_type,
                "valid": False,
                "errors": [
                    f"{exc} (resource_type={getattr(exc, 'resource_type', resource_type)}, "
                    f"missing_dependency={missing})"
                ],
                "transformed": None,
                "preview": True,
            }
        return {
            "resource_type": resource_type,
            "valid": True,
            "errors": [],
            "transformed": transformed,
            "preview": True,
        }
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.post("/imports", response_model=JobCreated, status_code=202)
def start_import(body: ImportRequest) -> JobCreated:
    """Import transformed resources to target AAP (mirrors ``import``)."""
    return submit_chained("import", body, services.run_import)


@router.post("/imports/check-dependencies")
def check_import_dependencies(body: ImportDependencyCheckRequest) -> dict:
    """Show dependency closure without importing (mirrors ``import --check-dependencies``)."""
    from aap_migration.cli.commands.export_import import (
        build_dependency_closure,
        get_missing_dependencies,
    )
    from aap_migration.resources import get_importable_types

    if body.job_id:
        from aap_migration.api.context import resolve_job_state

        # Same 404/409 semantics as submit-time chaining (unknown -> 404,
        # non-succeeded -> 409) instead of the generic 400 envelope.
        workdir, chained_state = resolve_job_state(
            body.job_id, strict=False, allow_statuses=("succeeded",)
        )
        assert workdir is not None  # job_id is truthy, so a dir is returned
        try:
            ctx = build_ephemeral_context(body.source_id, body.target_id)
            # Prefer the chained directory's transform tree when present.
            chained_transform = workdir / "xformed"
            if chained_transform.is_dir():
                ctx.config.paths.transform_dir = str(chained_transform)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        chained_state = None
        try:
            ctx = build_ephemeral_context(body.source_id, body.target_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.resource_types is None:
        requested: list[str] = get_importable_types(use_discovered=True)
    else:
        requested = list(body.resource_types)
    available = get_importable_types(use_discovered=True)
    closure = build_dependency_closure(requested, available)
    # Like POST /validations/dependencies: judge the chained pipeline against
    # its own state DB, not the ephemeral/default one. Read-only: fall back
    # to a throwaway temp-dir state (never the server-default DB on disk)
    # when no chained state resolves.
    from contextlib import ExitStack

    from aap_migration.api.context import (
        default_state_db_path,
        open_state,
        open_throwaway_state,
    )

    with ExitStack() as stack:
        if chained_state is not None:
            state = chained_state
        else:
            default_db = default_state_db_path()
            if default_db is not None:
                state = open_state(default_db)
            else:
                state = stack.enter_context(open_throwaway_state())
        missing = get_missing_dependencies(closure, state)
    return {
        "requested": requested,
        "dependency_closure": closure,
        "missing": missing,
    }


@router.post("/imports/patch-projects", response_model=JobCreated, status_code=202)
def start_patch_projects(body: PatchProjectsRequest) -> JobCreated:
    """Patch project SCM details, Phase 2 (mirrors ``patch-projects``)."""
    return submit_chained("patch-projects", body, services.run_patch_projects)


@router.post("/imports/granular", response_model=JobCreated, status_code=202)
def start_granular_import(body: GranularImportRequest) -> JobCreated:
    """Import micro-phase steps in order (mirrors the granular import menu)."""
    return submit_chained("granular-import", body, services.run_granular_import)
