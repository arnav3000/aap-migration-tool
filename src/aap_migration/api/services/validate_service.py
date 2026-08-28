"""Validate service — shared engine for CLI and API.

Both ``cli/commands/validate.py`` and ``api/routers/validate.py`` delegate
to the same engine (``validate/runner.py:run_validation`` and
``validate/report.py``) so CLI and web produce identical results.
This module isolates the API-side orchestration (config/state/client
construction, report rendering, job logging) without duplicating
comparison logic.

CLI can continue to call ``run_validation`` directly; API wraps the
same call in a background ``Job`` so the UI remains responsive.
If a future refactor is desired, CLI should be updated to call
``execute_validate`` here instead of inlining ``run_validation`` — the
interface is intentionally compatible.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from aap_migration.api.services.job_service import Job


def _build_validate_config(
    source_conn: Any | None = None,
    target_conn: Any | None = None,
    *,
    export_dir: str | None = None,
    report_dir: str | None = None,
    db_url: str | None = None,
) -> Any:
    """Build a ``MigrationConfig`` suitable for ``run_validation``.

    Uses connection URLs when provided; falls back to placeholder URLs
    (validate only needs URLs for report headers).  ``export_dir`` and
    ``report_dir`` default to the standard ``PathConfig`` values.
    """
    from aap_migration.api.services.connection_service import ConnectionService
    from aap_migration.config import MigrationConfig, PathConfig, StateConfig

    def _instance(conn: Any | None, fallback_url: str) -> Any:
        if conn is not None:
            return ConnectionService.build_instance_config(conn)
        # Minimal placeholder — never used for network calls unless live
        from aap_migration.config import AAPInstanceConfig

        return AAPInstanceConfig(url=fallback_url, token="placeholder-token", verify_ssl=True)

    source_cfg = _instance(source_conn, "https://source.example.com")
    target_cfg = _instance(target_conn, "https://target.example.com")

    paths = PathConfig()
    if export_dir:
        paths.export_dir = export_dir
    if report_dir:
        paths.report_dir = report_dir

    state = StateConfig(db_path=db_url) if db_url else StateConfig()

    return MigrationConfig(source=source_cfg, target=target_cfg, paths=paths, state=state)


def _build_migration_state(db_url: str, source_key: str = "") -> Any | None:
    """Return a ``MigrationState`` for the given DB URL, or None on failure."""
    try:
        from aap_migration.config import StateConfig
        from aap_migration.migration.state import MigrationState

        return MigrationState(StateConfig(db_path=db_url), source_key=source_key)
    except Exception:
        return None


async def execute_validate(
    *,
    config: Any,
    migration_state: Any | None,
    target_client: Any | None,
    live: bool,
    resource_type: str | None,
    skip_hosts: bool,
    organizations: list[str] | None,
    log: Callable[[str], None] | None = None,
) -> tuple[Any, dict | None, str | None]:
    """Run the shared validate engine and render HTML.

    Returns ``(ValidationResult, field_data, html)``.  ``html`` is the
    self-contained report from ``validate/report.py:generate_validation_html``.
    ``log`` is an optional progress sink (job log lines).
    """
    from aap_migration.validate.report import generate_validation_html
    from aap_migration.validate.runner import run_validation

    def _log(msg: str) -> None:
        if log:
            log(msg)

    _log(f"Validate start: live={live} resource_type={resource_type or 'all'} skip_hosts={skip_hosts} orgs={organizations or []}")

    result, field_data = await run_validation(
        config=config,
        migration_state=migration_state,
        target_client=target_client,
        live=live,
        resource_type=resource_type,
        skip_hosts=skip_hosts,
        organizations=organizations,
    )

    _log(f"Validate complete: types={len(result.per_type)} missing={result.executive_summary.total_missing_on_target} field_mm={result.executive_summary.total_field_mismatches} verdict={result.executive_summary.verdict}")

    html: str | None = None
    try:
        html = generate_validation_html(result, field_data)
        _log("Validation HTML report rendered")
    except Exception as exc:
        _log(f"Warning: HTML generation failed: {exc}")

    return result, field_data, html


def validate_job_coro_factory(
    *,
    live: bool,
    resource_type: str | None,
    skip_hosts: bool,
    organizations: list[str] | None,
    source_conn: Any | None,
    target_conn: Any | None,
    export_dir: str | None,
    output_dir: str | None,
    db_url: str,
) -> Callable[[Job, Callable[[str], None]], Any]:
    """Return a ``coro_factory`` for ``JobService.start_job`` that runs validate.

    The returned factory closes over all validate parameters so the router
    can simply call ``svc.start_job(..., validate_job_coro_factory(...))``
    without inlining engine details.
    """

    async def _do_validate(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        config = _build_validate_config(
            source_conn,
            target_conn,
            export_dir=export_dir,
            report_dir=output_dir,
            db_url=db_url,
        )
        # Migration state explains gaps in both live and DB modes
        migration_state = _build_migration_state(db_url)

        target_client = None
        if live:
            if target_conn is not None:
                from aap_migration.api.services.connection_service import ConnectionService

                target_client = ConnectionService.build_target_client(target_conn)
            else:
                # No explicit target connection — try to build from config (placeholder)
                # Live without a real target will fail with a clear error from run_validation
                from aap_migration.client.aap_target_client import AAPTargetClient

                target_client = AAPTargetClient(config.target)

        # Wrap target client lifecycle when live
        if target_client is not None:
            async with target_client:
                result, field_data, html = await execute_validate(
                    config=config,
                    migration_state=migration_state,
                    target_client=target_client,
                    live=live,
                    resource_type=resource_type,
                    skip_hosts=skip_hosts,
                    organizations=organizations,
                    log=log,
                )
        else:
            result, field_data, html = await execute_validate(
                config=config,
                migration_state=migration_state,
                target_client=None,
                live=live,
                resource_type=resource_type,
                skip_hosts=skip_hosts,
                organizations=organizations,
                log=log,
            )

        # Persist HTML on the job for the export endpoint (mirrors analysis router)
        job._html_report = html  # type: ignore[attr-defined]

        # Optionally write reports to disk when output_dir was requested (CLI parity)
        if output_dir:
            try:
                from aap_migration.validate.org_report import write_org_scoped_validation_reports

                base = Path(output_dir)
                written = write_org_scoped_validation_reports(
                    result,
                    base_dir=base,
                    live=live,
                    organizations=organizations or [],
                    resource_type=resource_type,
                    field_data=field_data,
                )
                for label, _jp, hp in written:
                    log(f"Report written ({label}): {hp}")
            except Exception as exc:
                log(f"Warning: writing reports to {output_dir} failed: {exc}")

        return {
            "mode": result.metadata.mode,
            "verdict": result.executive_summary.verdict,
            "per_type_count": len(result.per_type),
            "result": result.to_dict(),
            "field_data_available": field_data is not None,
        }

    return _do_validate
