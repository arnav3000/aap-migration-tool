"""Maintenance background workers: prep, cleanup, retry-failed, state export."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from aap_migration.api.context import open_default_state
from aap_migration.api.jobs import TERMINAL_STATUSES, JobRecord
from aap_migration.api.services._core import (
    _artifacts,
    _cancel_requested,
    _relativize,
    call_command,
    chained_ctx,
    is_noop_scope,
    noop_result,
    workdir_ctx,
)


# -- maintenance: prep / cleanup --------------------------------------------
def run_prep(job: JobRecord) -> dict[str, Any]:
    """Endpoint discovery + schema generation (mirrors ``prep``)."""
    from aap_migration.prep import (
        compare_schemas,
        discover_endpoints,
        generate_schema,
        save_comparison,
        save_endpoints,
        save_schema,
    )
    from aap_migration.utils.version_validation import (
        VersionValidationError,
        validate_version_compatibility,
    )

    with chained_ctx(job) as (ctx, config, workdir, params):
        out_dir = workdir / "schemas"

        async def _main() -> Any:
            await ctx.source_client.get("ping/")
            await ctx.target_client.get("ping/")
            source_version = await ctx.source_client.get_version()
            target_version = await ctx.target_client.get_version()
            try:
                validate_version_compatibility(source_version, target_version)
            except VersionValidationError as exc:
                raise ValueError(f"Version compatibility error: {exc}") from exc
            common_ignored = config.ignored_endpoints.get("common", [])
            source_endpoints = await discover_endpoints(
                ctx.source_client,
                api_version=source_version,
                ignored_endpoints=common_ignored + config.ignored_endpoints.get("source", []),
            )
            target_endpoints = await discover_endpoints(
                ctx.target_client,
                api_version=target_version,
                ignored_endpoints=common_ignored + config.ignored_endpoints.get("target", []),
            )
            save_endpoints(source_endpoints, out_dir / "source_endpoints.json")
            save_endpoints(target_endpoints, out_dir / "target_endpoints.json")
            source_schema = await generate_schema(ctx.source_client, source_endpoints)
            target_schema = await generate_schema(ctx.target_client, target_endpoints)
            save_schema(source_schema, out_dir / "source_schema.json")
            save_schema(target_schema, out_dir / "target_schema.json")
            comparison = compare_schemas(source_schema, target_schema)
            save_comparison(comparison, out_dir / "schema_comparison.json")
            return {
                "source_version": source_version,
                "target_version": target_version,
                "source_endpoints": len(source_endpoints.get("endpoints", {})),
                "target_endpoints": len(target_endpoints.get("endpoints", {})),
            }

        summary: dict[str, Any] = asyncio.run(_main())
    summary["message"] = "Prep complete"
    summary["artifacts"] = _artifacts(workdir, "schemas")
    return summary


def run_cleanup(job: JobRecord) -> dict[str, Any]:
    """Delete migrated resources + reset DB (mirrors ``cleanup``)."""
    if is_noop_scope(job["params"]):
        return noop_result()
    with chained_ctx(job) as (ctx, config, workdir, params):
        call_command(
            "cleanup",
            ctx,
            resource_type=tuple(params.get("resource_types") or ()),
            full=bool(params.get("full", False)),
            db_only=bool(params.get("db_only", False)),
            exports_dir=str(workdir / "exports"),
            force=True,
            yes=True,
            rate_limit=params.get("rate_limit"),
            skip_dir=tuple(params.get("skip_dir") or []),
        )
    return {"message": "Cleanup complete"}


def run_retry_failed(job: JobRecord) -> dict[str, Any]:
    """Retry failed imports (mirrors ``retry failed``).

    Retries each failed resource type separately, polling for operator
    cancel between types (mirroring :func:`etl.run_migrate` /
    :func:`etl.run_granular_import`). A cancel stops further types from
    starting; a type already running in its ``migrate`` subprocess runs to
    completion because the subprocess cannot be preempted (its timeout is
    owned by the CLI retry path, not this worker).

    With ``job_id``, retries against the chained job's workdir and isolated
    state DB (where API job failures are recorded); otherwise operates on
    the server-default state DB and the startup-CWD ``xformed/`` directory
    (CLI parity). The retry override (state forced to the resolved DB) is
    written to the chained workdir's config file so the ``retry`` command's
    internal ``migrate`` subprocesses (``--config``) resolve it.
    """
    from aap_migration.api.context import open_default_state, open_state, write_job_config

    if is_noop_scope(job["params"]):
        return noop_result()
    with chained_ctx(job, allow_statuses=TERMINAL_STATUSES, need="both") as (
        ctx,
        config,
        workdir,
        params,
    ):
        ref_id = params.get("job_id")
        if ref_id:
            # Fence-wait on the chained workdir (mirrors the FIFO dequeue
            # path): a retry onto a timed-out job's directory must not run
            # concurrently with the still-writing orphan.
            from aap_migration.api.jobs import get_job_manager

            if not get_job_manager().wait_for_unfence(str(workdir)):
                raise ValueError(
                    "Workdir fenced by a timed-out attempt still running; resubmit after it drains"
                )
            candidate = workdir / "migration_state.db"
            if not candidate.exists():
                raise ValueError(f"No migration state DB in job '{ref_id}' yet")
            target_state = open_state(str(candidate))
            server_xformed = workdir / "xformed"
            input_dir = server_xformed if server_xformed.is_dir() else None
        else:
            default_state = open_default_state()
            if default_state is None:
                raise ValueError("No migration state DB found; nothing to retry")
            target_state = default_state
            startup_cwd = Path(os.environ.get("AAP_BRIDGE_STARTUP_CWD", os.getcwd())).resolve()
            server_xformed = startup_cwd / "xformed"
            input_dir = server_xformed if server_xformed.is_dir() else None
        # Config file for the retry subprocesses (state forced to resolved DB).
        url = target_state.database_url
        config.state.db_path = url.split("sqlite:///")[-1] if "sqlite:///" in url else url
        write_job_config(config, workdir)
        ctx._config = config
        requested = tuple(params.get("resource_types") or ())
        if requested:
            rtypes = list(requested)
        else:
            get_failed_types = getattr(target_state, "get_failed_resource_types", None)
            try:
                rtypes = list(get_failed_types()) if callable(get_failed_types) else []
            except Exception:
                rtypes = []
        dry_run = bool(params.get("dry_run", False))
        if not rtypes:
            # Nothing discovered (or DB unreadable): single call preserves
            # CLI behavior ("No failed resources to retry!").
            call_command(
                "retry-failed",
                ctx,
                resource_type=(),
                input_dir=input_dir,
                dry_run=dry_run,
                yes=True,
            )
            return {"message": "Retry failed complete", "retried": []}
        retried: list[str] = []
        for rtype in rtypes:
            if _cancel_requested(job):
                break
            call_command(
                "retry-failed",
                ctx,
                resource_type=(rtype,),
                input_dir=input_dir,
                dry_run=dry_run,
                yes=True,
            )
            retried.append(rtype)
    if _cancel_requested(job):
        return {
            "message": "Retry failed cancelled",
            "retried": retried,
            "cancelled": True,
        }
    return {"message": "Retry failed complete", "retried": retried}


def run_state_export(job: JobRecord) -> dict[str, Any]:
    """Export the server-default migration state to a JSON backup file."""

    default_state = open_default_state()
    if default_state is None:
        raise ValueError("No migration state DB found; nothing to export")
    # Connectionless worker (pinless submit): workdir + logging lifecycle
    # only, no pair resolution.
    with workdir_ctx(job) as (workdir, _params):
        output = workdir / "reports" / "state-export.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        default_state.export_state(str(output))
        return {
            "message": "State export complete",
            "state_file": _relativize(str(output), workdir),
        }
