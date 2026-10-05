"""Shared worker lifecycle and helpers for API background jobs.

Split by job family into :mod:`etl`, :mod:`iam`, :mod:`credentials`,
:mod:`reporting`, and :mod:`maintenance`. Lifecycle home: :func:`chained_ctx`
(``setup_chained`` + ``close_job_context`` + ``teardown_job_logging``) is
the single setup/teardown path for connection-bearing workers; do not add
new ad-hoc setup/teardown scaffolding. :func:`workdir_ctx` is the companion
for connectionless workers (no pair, no snapshot pins) needing only the
workdir + logging lifecycle.

CLI <-> API bridge (see also schemas request models):

- Server-default scope shares the CLI DB: sync readers (state/show,
  mappings, retry/status, checkpoints, migrations/status) open the same
  file resolved by ``default_state_db_path`` (``MIGRATION_STATE_DB_PATH``,
  then ``./migration_state.db``, then ``./database/migration_state.db``
  under ``AAP_BRIDGE_STARTUP_CWD``). A GET never creates a DB.
- Job scope is isolated per job dir (``exports/``, ``xformed/``,
  ``migration_state.db``). To promote a job dir to CWD for CLI use:
  ``GET /jobs/{id}/artifacts`` to list, ``GET
  /jobs/{id}/artifacts/{path}`` to download (e.g. ``xformed/``,
  ``exports/``, reports), then manually copy the downloaded tree into
  the CLI working directory. There is no server-side "promote to CWD"
  operation by design (no CWD mutation on a shared server process).
- Credential-store split: the API stores AAP connections encrypted in
  the API DB (``AAP_BRIDGE_API_DB``) via ``/connections``; the CLI uses
  ``config.yaml``/``.env`` tokens. Workers resolve the stored pair at
  submit (snapshot pins) and execution (re-verify); CLI runs never read
  the API DB and API workers never read CLI ``config.yaml`` except for
  repo-level mappings/ignored-endpoints fallbacks.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypedDict, cast

import click

from aap_migration.api.context import setup_chained
from aap_migration.api.jobs import JobRecord
from aap_migration.api.store import NeedScope
from aap_migration.cli.context import MigrationContext


class JobParams(TypedDict, total=False):
    """Worker param keys (connection selectors, chaining, ETL options).

    Centralizes the stringly-typed ``params.get(...)`` keys previously
    duplicated across every ``run_*`` function. Request schemas remain the
    source of truth for defaults; workers read through this shape.
    """

    source_id: str | None
    target_id: str | None
    job_id: str | None
    allow_pair_switch: bool
    resource_types: list[str] | None
    resource_type: str | None
    force: bool
    resume: bool
    dry_run: bool
    live: bool
    quiet: bool
    disable_progress: bool
    skip_prep: bool
    skip_hosts: bool
    skip_dependencies: bool
    skip_pending_deletion: bool
    defer_project_sync: bool
    skip_user_roles: bool
    users_only: bool
    full: bool
    db_only: bool
    check_dependencies: bool
    force_reimport: bool
    by_organization: bool
    verify_ssl: bool | None
    phase: str | None
    from_phase: str | None
    json_path: str | None
    organization: str | None
    organizations: list[str]
    orgs: str | None
    output_format: str | None
    scan_strategy: str | None
    steps: list[str]
    skip_dir: list[str]
    batch_size: int | None
    interval: int | None
    rate_limit: int | None
    records_per_file: int | None
    sample_size: int | None
    timeout: int | None
    workers: int | None
    benchmark_workers: list[int] | None
    # Submit-time pair-fingerprint snapshot (see store.SNAPSHOT_*): pinned
    # at submit by submit_chained, verified at execution. Declared here so
    # a typo fails type-check instead of silently disabling the drift guard.
    _snapshot_source_id: str | None
    _snapshot_target_id: str | None
    _snapshot_fp: str | None
    _snapshot_need: str | None
    _snapshot_fernet_fp: str | None


def iam_max_workers(params: dict[str, Any]) -> int:
    """IAM worker count (single home for the int-shaped default)."""
    value = params.get("workers", 1)
    return int(value) if isinstance(value, int) else 1


def is_noop_scope(params: dict[str, Any]) -> bool:
    """True when the caller explicitly selected no resource types.

    Contract: omitted/None means all, explicit ``[]`` is a no-op. Workers
    check this first and return an empty result without invoking the CLI,
    so an uninitialized form field cannot trigger a full run.
    """
    return "resource_types" in params and params.get("resource_types") == []


def noop_result(message: str = "No resource types selected; nothing to do") -> dict[str, Any]:
    """Empty result envelope for explicit-[] no-op runs (single home)."""
    return {"message": message, "artifacts": []}


def benchmark_counts(params: dict[str, Any]) -> list[int]:
    """Benchmark worker-count sweep (single home for the list default)."""
    value = params.get("benchmark_workers", params.get("workers"))
    if isinstance(value, list) and value:
        return [int(v) for v in value]
    return [1, 10, 20]


def parse_organizations(params: dict[str, Any]) -> list[str] | None:
    """Canonical organization scope for reporting/validate workers (single home).

    Accepts legacy spellings at the boundary and returns the canonical
    ``organizations: list[str] | None`` where ``None`` means all (no filter).

    Accepted keys:

    - ``organizations``: ``list[str]`` (canonical) or comma-separated ``str``.
    - ``organization``: single ``str`` (or single-element ``list``).
    - ``orgs``: comma-separated ``str`` (CLI ``--orgs``) or ``list[str]``.

    Rules (fail closed, never silently widen to all):

    - No known org keys present (all missing/``None``/empty-string) -> ``None``.
    - Exactly one spelling present with values -> normalized ``list[str]``.
      Empty ``list`` is preserved as ``[]`` (explicit empty); empty/blank
      ``str`` normalizes to ``None`` (mirrors ``parse_orgs_arg``).
    - Multiple spellings present with values -> ``ValueError`` (ambiguous;
      specify only one spelling) instead of silently picking one.
    - Unknown ``*org*`` keys (e.g. ``organisations``, ``org``) holding a
      non-empty value -> ``ValueError`` instead of widening to ``None``
      (all). ``by_organization``/``analyze_all`` are exempt (flags, not
      scope values).

    Callers keep their own scope validation (e.g. analyze ``analyze_all``
    vs orgs exclusivity); this helper only normalizes the spelling.
    """
    _KNOWN = ("organizations", "organization", "orgs")
    _EXEMPT = {"analyze_all", "by_organization"}
    # Fail closed on typo spellings: an unknown *org* key with a real value
    # must not silently become None (= all organizations).
    for key, value in params.items():
        if "org" not in key.lower():
            continue
        if key in _KNOWN or key in _EXEMPT:
            continue
        if value is None:
            continue
        if isinstance(value, list | tuple) and len(value) == 0:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        raise ValueError(
            f"Unknown organization field {key!r}; "
            "use one of 'organizations', 'organization', or 'orgs'."
        )

    def _norm_list(value: Any) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",") if p.strip()]
            return parts if parts else None
        if isinstance(value, list | tuple):
            cleaned = [str(v).strip() for v in value if str(v).strip()]
            if len(value) == 0:
                return []
            return cleaned if cleaned else []
        return None

    def _norm_single(value: Any) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return [stripped] if stripped else None
        if isinstance(value, list | tuple):
            return _norm_list(value)
        return None

    normed: dict[str, list[str] | None] = {
        "organizations": _norm_list(params.get("organizations"))
        if "organizations" in params
        else None,
        "organization": _norm_single(params.get("organization"))
        if "organization" in params
        else None,
        "orgs": _norm_list(params.get("orgs")) if "orgs" in params else None,
    }
    present = {k: v for k, v in normed.items() if v is not None}
    # Treat explicit [] as "no values" for ambiguity: default [] plus one
    # real spelling is not ambiguous (mirrors Analyze default).
    with_values = {k: v for k, v in present.items() if len(v) > 0}
    if len(with_values) > 1:
        raise ValueError(
            "Ambiguous organization scope: specify only one of "
            "'organizations', 'organization', or 'orgs'."
        )
    if with_values:
        return next(iter(with_values.values()))
    for v in present.values():
        if v == []:
            return []
    return None


@contextmanager
def workdir_ctx(
    job: JobRecord,
    allow_statuses: tuple[str, ...] = ("succeeded",),
) -> Iterator[tuple[Path, JobParams]]:
    """Workdir + logging lifecycle for connectionless workers (see #7).

    Companions :func:`chained_ctx` for workers that need no AAP pair and
    carry no snapshot pins (IAM report re-rendering, server-default state
    export): resolves the workdir honoring ``job_id`` chaining, attaches
    the per-job log file, and tears both down on exit. Connection-bearing
    workers must use :func:`chained_ctx` instead so pair pinning,
    pair-switch forks, and execution-time SSRF re-verification apply.
    """
    raw = job["params"]
    if not isinstance(raw, dict):
        raise TypeError(f"job params must be a dict, got {type(raw).__name__}")
    params = cast(JobParams, dict(raw))
    from aap_migration.api.context import resolve_workdir, setup_job_logging

    workdir = resolve_workdir(raw, job["job_dir"], allow_statuses=allow_statuses)
    setup_job_logging(workdir)
    try:
        yield workdir, params
    finally:
        from aap_migration.api.context import teardown_job_logging

        teardown_job_logging(workdir)


@contextmanager
def chained_ctx(
    job: JobRecord,
    allow_statuses: tuple[str, ...] = ("succeeded",),
    need: NeedScope = "both",
) -> Iterator[tuple[MigrationContext, Any, Path, JobParams]]:
    """Single home for worker setup/teardown (see #29).

    Builds ``(ctx, config, workdir)`` honoring ``job_id`` chaining, yields
    them with params typed as :class:`JobParams`, and closes HTTP clients on
    exit. Replaces the copy-pasted ``setup_chained`` + ``try/finally close`` scaffolding in every ``run_*`` function.
    """
    raw = job["params"]
    if not isinstance(raw, dict):
        raise TypeError(f"job params must be a dict, got {type(raw).__name__}")
    if need != "none":
        # Assert the submit-time pair-fingerprint keys once here instead of
        # trusting every params.get downstream: a typo or missing pin would
        # otherwise pass type-check silently and disable the drift guard.
        # _snapshot_fernet_fp is best-effort (pre-fingerprint records
        # skip the rotation check), so it stays optional here.
        for _key in (
            "_snapshot_source_id",
            "_snapshot_target_id",
            "_snapshot_fp",
            "_snapshot_need",
        ):
            if _key not in raw:
                raise KeyError(f"job params missing required pin key {_key!r}")
    params = cast(JobParams, dict(raw))
    ctx, config, workdir = setup_chained(
        raw, job["job_dir"], allow_statuses=allow_statuses, need=need
    )
    if Path(workdir).resolve() != Path(job["job_dir"]).resolve():
        # Pair-switch fork (see #4): point the job record at the fresh
        # sibling dir so later chained phases and the artifact APIs follow
        # the fork instead of the stale parent dir.
        from aap_migration.api.jobs import get_job_manager

        get_job_manager().set_job_dir(job["job_id"], str(workdir))
    try:
        yield ctx, config, workdir, params
    finally:
        from aap_migration.api.context import close_job_context, teardown_job_logging

        close_job_context(ctx)
        teardown_job_logging(workdir)


# -- helpers ------------------------------------------------------------
def _cancel_requested(job: JobRecord) -> bool:
    """Return True when an operator cancelled this job mid-run.

    Workers poll this between steps/phases so a cancel stops further writes
    instead of running every remaining step and then reporting cancelled.
    The FIFO worker still owns the final status transition.
    """
    try:
        from aap_migration.api.jobs import get_job_manager

        return bool(get_job_manager().get_internal(job["job_id"]).get("cancel_requested"))
    except Exception as exc:
        # Fail open (do not spuriously cancel live work on a transient read
        # error) but stay observable: a skipped cancel check must appear in
        # server logs instead of silently running revoked steps as success.
        logging.getLogger("aap_migration.api.services").warning(
            "cancel-flag read failed for job %s (%r); treating as not-cancelled",
            job.get("job_id"),
            exc,
        )
        return False


def _service_command_registry() -> dict[str, click.Command]:
    """Explicit allowlist of CLI commands invokable from API workers.

    Single home for the stringly-typed service -> CLI mapping. Imports are
    function-local so ``api.services`` stays importable without pulling the
    whole CLI package at module load.
    """
    from aap_migration.cli.commands.cleanup import cleanup as cleanup_cmd
    from aap_migration.cli.commands.export_import import export as export_cmd
    from aap_migration.cli.commands.export_import import import_cmd
    from aap_migration.cli.commands.migrate import resume as resume_cmd
    from aap_migration.cli.commands.migration_report import (
        generate_migration_report as migration_report_cmd,
    )
    from aap_migration.cli.commands.migration_report_v2 import (
        generate_enhanced_report as enhanced_report_cmd,
    )
    from aap_migration.cli.commands.patch_projects import (
        patch_projects as patch_projects_cmd,
    )
    from aap_migration.cli.commands.prep import prep as prep_cmd
    from aap_migration.cli.commands.project_failures import (
        analyze_project_failures as project_failures_cmd,
    )
    from aap_migration.cli.commands.retry import retry_failed as retry_failed_cmd
    from aap_migration.cli.commands.transform import transform as transform_cmd

    return {
        "export": export_cmd,
        "import": import_cmd,
        "transform": transform_cmd,
        "patch-projects": patch_projects_cmd,
        "prep": prep_cmd,
        "resume": resume_cmd,
        "cleanup": cleanup_cmd,
        "retry-failed": retry_failed_cmd,
        "migration-report": migration_report_cmd,
        "enhanced-report": enhanced_report_cmd,
        "analyze-project-failures": project_failures_cmd,
    }


def call_command(cmd_name: str, ctx: MigrationContext, **kwargs: Any) -> Any:
    """Invoke a registered CLI command from an API worker (typed thin layer).

    Replaces ad-hoc ``click.Context(...)`` fabrication at every call site:
    the command is resolved from an explicit allowlist by name, inputs are
    type-checked, and the command's own root context is built once here.

    Args:
        cmd_name: Registered command name (see :func:`_service_command_registry`).
        ctx: Migration context shared with the worker.
        **kwargs: Command parameters; keys must match the command's declared
            click params, values are passed through unchanged.

    Returns:
        Whatever the command callback returns (CLI commands return None).

    Raises:
        TypeError: If ``cmd_name`` is not a string, ``ctx`` is not a
            :class:`MigrationContext`, or unknown parameter names are passed.
        ValueError: If ``cmd_name`` is not registered.
    """
    if not isinstance(cmd_name, str):
        raise TypeError(f"cmd_name must be str, got {type(cmd_name).__name__}")
    registry = _service_command_registry()
    if cmd_name not in registry:
        raise ValueError(
            f"Unknown service command {cmd_name!r}. Available: {', '.join(sorted(registry))}"
        )
    if not isinstance(ctx, MigrationContext):
        raise TypeError(f"ctx must be MigrationContext, got {type(ctx).__name__}")
    cmd = registry[cmd_name]
    allowed = {param.name for param in cmd.params if param.expose_value}
    unknown = sorted(set(kwargs) - allowed)
    if unknown:
        raise TypeError(
            f"Unknown parameter(s) for command {cmd_name!r}: {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(allowed))}"
        )
    click_ctx = click.Context(cmd, info_name=cmd.name or cmd_name, obj=ctx)
    return click_ctx.invoke(cmd, **kwargs)


_ARTIFACTS_RESULT_CAP = 500


def _artifact_result(
    job_dir: Path | str, *names: str, cap: int = _ARTIFACTS_RESULT_CAP
) -> dict[str, object]:
    """Bounded artifacts payload with truncated/total metadata (P3 #31).

    Single home for result artifact payloads: one directory walk
    accumulates the capped list and the true pre-page total together, so
    job completion never pays two full traversals (the old split walked
    once for the total, then again for the capped list, defeating the
    cap's I/O purpose). Returns ``{"artifacts", "artifacts_total",
    "artifacts_truncated"}`` for ``**``-spread into result payloads;
    index ``["artifacts"]`` when only the bounded list is needed.
    """
    base = job_dir if isinstance(job_dir, Path) else Path(str(job_dir))
    found: list[str] = []
    total = 0
    for name in names:
        path = base / name
        if not path.exists():
            continue
        if path.is_dir():
            for p in sorted(path.rglob("*")):
                if p.is_file():
                    total += 1
                    if len(found) < cap:
                        found.append(str(p.relative_to(base)))
        else:
            total += 1
            if len(found) < cap:
                found.append(str(path.relative_to(base)))
    return {
        "artifacts": found,
        "artifacts_total": total,
        "artifacts_truncated": total > len(found),
    }


def _relativize(value: Any, workdir: Path) -> Any:
    """Rewrite absolute server-local paths under *workdir* to relative ones.

    Applied to job ``result`` payloads so public records never expose
    server-local filesystem layout (CWE-209). Non-path values pass through.
    """
    base = os.path.abspath(workdir)
    if isinstance(value, str):
        target = os.path.abspath(value) if os.path.isabs(value) else None
        if target is not None:
            try:
                if os.path.commonpath([target, base]) == base:
                    return os.path.relpath(target, base)
            except ValueError:
                pass
        return value
    if isinstance(value, dict):
        return {k: _relativize(v, workdir) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_relativize(v, workdir) for v in value]
    return value


def _resolve_iam_tls(params: dict[str, Any], stored: dict[str, Any]) -> tuple[bool, int]:
    """Resolve verify_ssl/timeout: explicit params win, else stored values."""
    verify = params.get("verify_ssl")
    if verify is None:
        verify = stored.get("verify_ssl", True)
    timeout = params.get("timeout")
    if timeout is None:
        timeout = stored.get("timeout", 60)
    return bool(verify), int(timeout)
