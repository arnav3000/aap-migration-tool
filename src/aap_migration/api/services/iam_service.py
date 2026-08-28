"""IAM service — shared engine for CLI and API.

Both ``cli/commands/iam.py`` and ``api/routers/iam.py`` delegate to the
same engine:

- ``iam/analyser.py:IAMAnalyser`` for audit / migrate
- ``iam/report.py`` for HTML / JSON reports
- ``iam/benchmark.py:run_benchmark`` for benchmarking

This module isolates API-side orchestration (connection resolution,
background job execution, log capture) without duplicating IAM logic.
CLI continues to call the engine directly; API wraps the same calls in
background ``Job`` objects.  A future refactor could make CLI delegate
here as well — the engine interface is intentionally stable.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Callable

from aap_migration.api.services.job_service import Job


def _resolve_state_db_path(explicit: str | None, db_url: str | None) -> str | None:
    """Resolve the IAM state DB file path from request or server DB URL."""
    if explicit:
        return explicit
    if not db_url:
        return None
    # IAMAnalyser._load_id_mappings expects a file path, not a full DSN.
    # For sqlite URLs unwrap; for postgres return None (name-based fallback).
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///") :]
    if db_url.startswith("postgresql://"):
        # IAM name-based fallback is acceptable; postgres mappings are not file-backed.
        return None
    return db_url


def _build_analyser_kwargs(
    source_conn: Any,
    target_conn: Any | None = None,
    *,
    verify_ssl: bool = True,
    timeout: int = 60,
    workers: int = 1,
    scan_strategy: str = "resource",
    state_db_path: str | None = None,
    checkpoint_path: str | None = None,
    resume: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    from aap_migration.api.crypto import decrypt_token

    source_token = decrypt_token(source_conn.token) if getattr(source_conn, "token", None) else ""
    kwargs: dict[str, Any] = {
        "source_url": source_conn.url,
        "source_token": source_token,
        "verify_ssl": verify_ssl,
        "request_timeout": timeout,
        "max_workers": workers,
        "scan_strategy": scan_strategy,
        "progress_callback": log or (lambda msg: None),
    }
    if checkpoint_path:
        kwargs["checkpoint_path"] = checkpoint_path
        kwargs["resume"] = resume
    elif resume:
        # No explicit checkpoint dir — use temp location (ephemeral)
        kwargs["checkpoint_path"] = os.path.join(tempfile.gettempdir(), "iam_checkpoint.json")
        kwargs["resume"] = resume

    if target_conn is not None:
        target_token = decrypt_token(target_conn.token) if getattr(target_conn, "token", None) else ""
        kwargs["target_url"] = target_conn.url
        kwargs["target_token"] = target_token

    if state_db_path:
        kwargs["state_db_path"] = state_db_path

    return kwargs


async def execute_iam_audit(
    source_conn: Any,
    *,
    verify_ssl: bool = True,
    timeout: int = 60,
    workers: int = 1,
    scan_strategy: str = "resource",
    resume: bool = False,
    checkpoint_dir: str | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the shared IAM audit engine and return serialised result with HTML."""
    import asyncio

    from aap_migration.iam.analyser import IAMAnalyser
    from aap_migration.iam.report import generate_iam_html_report

    checkpoint_path = os.path.join(checkpoint_dir, "iam_checkpoint.json") if checkpoint_dir else None

    def _run_sync() -> Any:
        kwargs = _build_analyser_kwargs(
            source_conn,
            verify_ssl=verify_ssl,
            timeout=timeout,
            workers=workers,
            scan_strategy=scan_strategy,
            checkpoint_path=checkpoint_path,
            resume=resume,
            log=log,
        )
        with IAMAnalyser(**kwargs) as analyser:
            return analyser.audit()

    if log:
        log(f"IAM audit start: strategy={scan_strategy} workers={workers} resume={resume}")

    # IAM analyser is synchronous (requests) — run off the event loop
    result = await asyncio.to_thread(_run_sync)

    if log:
        log(f"IAM audit complete: permissions={result.stats.permissions_found} resources={result.stats.resources_scanned}")

    html: str | None = None
    try:
        html = generate_iam_html_report(result)
    except Exception as exc:
        if log:
            log(f"Warning: IAM HTML generation failed: {exc}")

    return {"result": result, "html": html}


async def execute_iam_migrate(
    source_conn: Any,
    target_conn: Any,
    *,
    state_db_path: str | None = None,
    db_url: str | None = None,
    verify_ssl: bool = True,
    timeout: int = 60,
    workers: int = 1,
    scan_strategy: str = "resource",
    dry_run: bool = False,
    skip_user_roles: bool = False,
    users_only: bool = False,
    resume: bool = False,
    checkpoint_dir: str | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the shared IAM migrate engine."""
    import asyncio

    from aap_migration.iam.analyser import IAMAnalyser
    from aap_migration.iam.report import generate_iam_html_report

    effective_state_db = _resolve_state_db_path(state_db_path, db_url)
    checkpoint_path = os.path.join(checkpoint_dir, "iam_checkpoint.json") if checkpoint_dir else None

    if skip_user_roles and users_only:
        raise ValueError("--skip-user-roles and --users-only are mutually exclusive")

    def _run_sync() -> Any:
        kwargs = _build_analyser_kwargs(
            source_conn,
            target_conn,
            verify_ssl=verify_ssl,
            timeout=timeout,
            workers=workers,
            scan_strategy=scan_strategy,
            state_db_path=effective_state_db,
            checkpoint_path=checkpoint_path,
            resume=resume,
            log=log,
        )
        with IAMAnalyser(**kwargs) as analyser:
            return analyser.migrate(
                dry_run=dry_run,
                skip_user_roles=skip_user_roles,
                users_only=users_only,
            )

    if log:
        label = "dry-run" if dry_run else "migrate"
        log(f"IAM {label} start: strategy={scan_strategy} workers={workers} skip_user_roles={skip_user_roles} users_only={users_only}")

    result = await asyncio.to_thread(_run_sync)

    if log:
        log(
            f"IAM migrate complete: migrated={result.stats.permissions_migrated} "
            f"failed={result.stats.permissions_failed} pending={result.stats.user_permissions_pending}"
        )

    html: str | None = None
    try:
        html = generate_iam_html_report(result)
    except Exception as exc:
        if log:
            log(f"Warning: IAM HTML generation failed: {exc}")

    return {"result": result, "html": html}


def iam_audit_job_coro_factory(
    source_conn: Any,
    *,
    verify_ssl: bool = True,
    timeout: int = 60,
    workers: int = 1,
    scan_strategy: str = "resource",
    resume: bool = False,
    checkpoint_dir: str | None = None,
) -> Callable[[Job, Callable[[str], None]], Any]:
    async def _do_audit(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        payload = await execute_iam_audit(
            source_conn,
            verify_ssl=verify_ssl,
            timeout=timeout,
            workers=workers,
            scan_strategy=scan_strategy,
            resume=resume,
            checkpoint_dir=checkpoint_dir,
            log=log,
        )
        result = payload["result"]
        html = payload["html"]
        job._html_report = html  # type: ignore[attr-defined]
        return {
            "mode": result.mode,
            "source_url": result.source_url,
            "statistics": result.stats.to_dict(),
            "result": result.to_dict(),
        }

    return _do_audit


def iam_migrate_job_coro_factory(
    source_conn: Any,
    target_conn: Any,
    *,
    state_db_path: str | None = None,
    db_url: str | None = None,
    verify_ssl: bool = True,
    timeout: int = 60,
    workers: int = 1,
    scan_strategy: str = "resource",
    dry_run: bool = False,
    skip_user_roles: bool = False,
    users_only: bool = False,
    resume: bool = False,
    checkpoint_dir: str | None = None,
) -> Callable[[Job, Callable[[str], None]], Any]:
    async def _do_migrate(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        payload = await execute_iam_migrate(
            source_conn,
            target_conn,
            state_db_path=state_db_path,
            db_url=db_url,
            verify_ssl=verify_ssl,
            timeout=timeout,
            workers=workers,
            scan_strategy=scan_strategy,
            dry_run=dry_run,
            skip_user_roles=skip_user_roles,
            users_only=users_only,
            resume=resume,
            checkpoint_dir=checkpoint_dir,
            log=log,
        )
        result = payload["result"]
        html = payload["html"]
        job._html_report = html  # type: ignore[attr-defined]
        return {
            "mode": result.mode,
            "source_url": result.source_url,
            "statistics": result.stats.to_dict(),
            "result": result.to_dict(),
        }

    return _do_migrate
