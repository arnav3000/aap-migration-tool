"""ETL background workers: migrate, export, transform, import, patch-projects, granular import."""

from __future__ import annotations

from typing import Any

from aap_migration.api.jobs import TERMINAL_STATUSES, JobRecord
from aap_migration.api.services._core import (
    _artifact_result,
    _cancel_requested,
    call_command,
    chained_ctx,
    is_noop_scope,
    noop_result,
)
from aap_migration.migration.importers._registry import DEFAULT_GRANULAR_STEPS


# -- full migration workflow --------------------------------------------
def run_migrate(job: JobRecord) -> dict[str, Any]:
    """Run prep -> export -> transform -> import (mirrors ``migrate`` group)."""
    from aap_migration.cli.commands.migrate import _run_migration_workflow

    if is_noop_scope(job["params"]):
        return noop_result()
    if _cancel_requested(job):
        return {"message": "Migration cancelled before start", "cancelled": True}
    with chained_ctx(job) as (ctx, _, workdir, params):
        # Cancel is polled between workflow phases (the CLI hook below):
        # a cancel landing mid-run stops before the next mutating phase
        # instead of completing every write and then reporting cancelled.
        # The FIFO worker still owns the final status transition. Granular
        # import checks between steps the same way.
        _run_migration_workflow(
            ctx,
            resource_type=tuple(params.get("resource_types") or ()),
            force=bool(params.get("force", False)),
            resume=bool(params.get("resume", False)),
            skip_prep=bool(params.get("skip_prep", False)),
            phase=str(params.get("phase", "all")),
            should_abort=lambda: _cancel_requested(job),
            base_dir=workdir,
        )
    return {
        "message": "Migration workflow complete",
        **_artifact_result(workdir, "exports", "xformed", "schemas", "reports"),
    }


def run_migrate_resume(job: JobRecord) -> dict[str, Any]:
    """Resume a migration (mirrors ``migrate resume``)."""
    with chained_ctx(job, allow_statuses=TERMINAL_STATUSES) as (
        ctx,
        _,
        workdir,
        params,
    ):
        call_command(
            "resume",
            ctx,
            from_phase=params.get("from_phase"),
            yes=True,
            disable_progress=bool(params.get("disable_progress", False)),
            quiet=bool(params.get("quiet", False)),
        )
    return {"message": "Migration resume complete"}


# -- export / transform / import -----------------------------------------
def run_export(job: JobRecord) -> dict[str, Any]:
    """Export RAW resources (mirrors ``export``)."""
    if is_noop_scope(job["params"]):
        return noop_result()
    with chained_ctx(job) as (ctx, config, workdir, params):
        call_command(
            "export",
            ctx,
            output=workdir / "exports",
            resource_type=tuple(params.get("resource_types") or ()),
            force=True,
            records_per_file=params.get("records_per_file") or config.export.records_per_file,
            resume=bool(params.get("resume", False)),
            yes=True,
        )
    return {
        "message": "Export complete",
        **_artifact_result(workdir, "exports"),
    }


def run_transform(job: JobRecord) -> dict[str, Any]:
    """Transform RAW exports (mirrors ``transform``)."""
    if is_noop_scope(job["params"]):
        return noop_result()
    with chained_ctx(job) as (ctx, _, workdir, params):
        schema_candidate = workdir / "schemas" / "schema_comparison.json"
        call_command(
            "transform",
            ctx,
            input_dir=workdir / "exports",
            output_dir=workdir / "xformed",
            schema_file=schema_candidate if schema_candidate.exists() else None,
            force=True,
            resource_type=tuple(params.get("resource_types") or ()),
            quiet=bool(params.get("quiet", False)),
            disable_progress=bool(params.get("disable_progress", False)),
            skip_pending_deletion=bool(params.get("skip_pending_deletion", True)),
            defer_project_sync=bool(params.get("defer_project_sync", True)),
            yes=True,
        )
    return {
        "message": "Transform complete",
        **_artifact_result(workdir, "xformed"),
    }


def run_import(job: JobRecord) -> dict[str, Any]:
    """Import transformed resources (mirrors ``import``)."""
    if is_noop_scope(job["params"]):
        return noop_result()
    with chained_ctx(job) as (ctx, _, workdir, params):
        call_command(
            "import",
            ctx,
            input_dir=workdir / "xformed",
            resource_type=tuple(params.get("resource_types") or ()),
            force=True,
            resume=bool(params.get("resume", False)),
            dry_run=bool(params.get("dry_run", False)),
            skip_dependencies=bool(params.get("skip_dependencies", False)),
            check_dependencies=bool(params.get("check_dependencies", False)),
            force_reimport=bool(params.get("force_reimport", False)),
            phase=params.get("phase", "all"),
            yes=True,
        )
    return {"message": "Import complete"}


def run_patch_projects(job: JobRecord) -> dict[str, Any]:
    """Patch project SCM details, Phase 2 (mirrors ``patch-projects``)."""
    with chained_ctx(job) as (ctx, config, workdir, params):
        call_command(
            "patch-projects",
            ctx,
            input_dir=workdir / "xformed",
            batch_size=params.get("batch_size") or config.performance.project_patch_batch_size,
            interval=params.get("interval")
            if params.get("interval") is not None
            else config.performance.project_patch_batch_interval,
        )
    return {"message": "Patch projects complete"}


def run_granular_import(job: JobRecord) -> dict[str, Any]:
    """Import micro-phase steps in order (mirrors granular import menu).

    Steps run with ``resume=True`` so a retry after a partial failure resumes
    from persisted state instead of duplicating already-imported objects with
    ``force=True`` from scratch.
    """
    with chained_ctx(job) as (ctx, _, workdir, params):
        raw_steps = params.get("steps")
        if raw_steps is None:
            # Canonical granular order (single home: importers._registry).
            steps = list(DEFAULT_GRANULAR_STEPS)
        else:
            # Explicit [] is a no-op (consistent with resource_types):
            # run zero steps instead of the full order.
            steps = list(raw_steps)
        completed = []
        for step in steps:
            if _cancel_requested(job):
                break
            call_command(
                "import",
                ctx,
                input_dir=workdir / "xformed",
                resource_type=(step,),
                force=True,
                resume=True,
                dry_run=bool(params.get("dry_run", False)),
                skip_dependencies=False,
                check_dependencies=False,
                force_reimport=False,
                phase="all",
                yes=True,
            )
            completed.append(step)
    if _cancel_requested(job):
        return {
            "message": "Granular import cancelled",
            "steps_completed": completed,
            "cancelled": True,
        }
    return {"message": "Granular import complete", "steps_completed": completed}
