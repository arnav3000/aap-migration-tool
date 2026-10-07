"""Migration ETL endpoints.

Mirrors: ``migrate`` (full workflow + ``status`` + ``resume``), ``export``,
``transform``, ``import``, ``patch-projects``, and the granular import menu.
Long operations run as background jobs; status/statistics are synchronous.
"""

from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, HTTPException, Query, Request

from aap_migration.api import services
from aap_migration.api.jobs import TERMINAL_STATUSES
from aap_migration.api.routers._common import _get_state, submit_chained
from aap_migration.api.schemas import (
    ExportRequest,
    GranularImportRequest,
    ImportDepCheckOut,
    ImportDependencyCheckRequest,
    ImportRequest,
    JobCreated,
    MigrateRequest,
    MigrateResumeRequest,
    MigrationStatusOut,
    PatchProjectsRequest,
    PayloadCheckRequest,
    TransformPreviewOut,
    TransformRequest,
)

router = APIRouter(tags=["migrations"])


@router.post("/migrations", response_model=JobCreated, status_code=202)
def start_migration(body: MigrateRequest, request: Request) -> JobCreated:
    """Run the full workflow: prep -> export -> transform -> import."""
    return submit_chained(
        "migrate", body, services.run_migrate, root_path=request.scope.get("root_path", "")
    )


@router.get("/migrations/status", response_model=MigrationStatusOut)
def migration_status(
    job_id: str | None = Query(
        default=None, description="When set, read the chained job's state DB"
    ),
    strict: bool = Query(
        default=False,
        description="When true, return 404 instead of 200+warning when no state DB exists",
    ),
) -> dict:
    """Show migration status (mirrors ``migrate status``).

    The read is scoped by ``job_id`` or the server-default DB: connection
    ids are resolved at submit time via snapshot pins, so per-request
    ``source_id``/``target_id`` scoping is not supported here (removed;
    pass ``job_id`` to read a chained job's DB).

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

    state, missing = _get_state(
        job_id,
        strict,
        {
            "migration_id": None,
            "resource_stats": {},
            "unreadable_types": [],
            "warning": "No migration state DB found",
        },
    )
    if missing is not None:
        return missing
    assert state is not None

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
def resume_migration(body: MigrateResumeRequest, request: Request) -> JobCreated:
    """Resume from the last checkpoint (mirrors ``migrate resume``).

    Unlike other ETL phases, resume chains onto ``failed`` and ``cancelled``
    jobs (the ones that need resuming) as well as ``succeeded`` ones; only
    ``queued``/``running`` references are rejected with 409. The
    ``from_phase`` value-domain check lives in the schema (422 by
    construction); lifecycle conflicts stay 409 and missing prerequisites
    stay 400, so each failure class has one code.
    """
    return submit_chained(
        "migrate-resume",
        body,
        services.run_migrate_resume,
        allow_statuses=TERMINAL_STATUSES,
        root_path=request.scope.get("root_path", ""),
    )


@router.post("/exports", response_model=JobCreated, status_code=202)
def start_export(body: ExportRequest, request: Request) -> JobCreated:
    """Export RAW resources from source AAP (mirrors ``export``)."""
    return submit_chained(
        "export", body, services.run_export, root_path=request.scope.get("root_path", "")
    )


@router.post("/transforms", response_model=JobCreated, status_code=202)
def start_transform(body: TransformRequest, request: Request) -> JobCreated:
    """Transform RAW exports (mirrors ``transform``)."""
    return submit_chained(
        "transform", body, services.run_transform, root_path=request.scope.get("root_path", "")
    )


@router.post("/transforms/preview", response_model=TransformPreviewOut)
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
    from aap_migration.resources import get_info, normalize_resource_type

    resource_type = normalize_resource_type(body.resource_type)
    try:
        get_info(resource_type)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"Unknown resource type '{body.resource_type}'"
        ) from exc

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
def start_import(body: ImportRequest, request: Request) -> JobCreated:
    """Import transformed resources to target AAP (mirrors ``import``)."""
    return submit_chained(
        "import", body, services.run_import, root_path=request.scope.get("root_path", "")
    )


@router.post("/imports/check-dependencies", response_model=ImportDepCheckOut)
def check_import_dependencies(body: ImportDependencyCheckRequest) -> dict:
    """Show dependency closure without importing (mirrors ``import --check-dependencies``)."""
    from aap_migration.cli.commands.export_import import (
        build_dependency_closure,
        get_missing_dependencies,
    )
    from aap_migration.resources import get_importable_types

    if body.job_id:
        from aap_migration.api.routers._common import resolve_chained_scope

        # Same 404/409 semantics as submit-time chaining. State-based
        # closure: absent xformed with present DB still judges the chained
        # DB (preserves test_chained_dependency_reads_job_db); the
        # validation route owns the strict xformed->404 gate.
        _, chained_state, _ = resolve_chained_scope(
            body.job_id,
            body.source_id,
            body.target_id,
            require_xformed=False,
        )
    else:
        # Server-default scope is state-based and targetless: no
        # connection resolution needed (source-only deployments).
        chained_state = None
    if body.resource_types is None:
        requested: list[str] = get_importable_types(use_discovered=True)
    else:
        requested = list(body.resource_types)
    available = get_importable_types(use_discovered=True)
    closure = build_dependency_closure(requested, available)
    # Like POST /validations/dependencies: judge the chained pipeline against
    # its own state DB, not the ephemeral/default one. Server-default scope
    # is read-only: use the default DB when present, else a throwaway
    # temp-dir state (never create a DB as a side effect). Job scope never
    # falls back (404 above).
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
def start_patch_projects(body: PatchProjectsRequest, request: Request) -> JobCreated:
    """Patch project SCM details, Phase 2 (mirrors ``patch-projects``)."""
    return submit_chained(
        "patch-projects",
        body,
        services.run_patch_projects,
        root_path=request.scope.get("root_path", ""),
    )


@router.post("/imports/granular", response_model=JobCreated, status_code=202)
def start_granular_import(body: GranularImportRequest, request: Request) -> JobCreated:
    """Import micro-phase steps in order (mirrors the granular import menu)."""
    return submit_chained(
        "granular-import",
        body,
        services.run_granular_import,
        root_path=request.scope.get("root_path", ""),
    )
