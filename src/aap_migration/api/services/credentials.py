"""Credential background workers: compare, migrate, report."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from aap_migration.api.jobs import JobRecord
from aap_migration.api.services._core import _artifacts, _relativize, chained_ctx


# -- credentials ----------------------------------------------------------
def _credential_coordinator(ctx: Any) -> Any:
    """Single home for the credential MigrationCoordinator construction."""
    from aap_migration.migration.coordinator import MigrationCoordinator

    return MigrationCoordinator(
        config=ctx.config,
        source_client=ctx.source_client,
        target_client=ctx.target_client,
        state=ctx.migration_state,
        enable_progress=False,
    )


def run_credential_compare(job: JobRecord) -> dict[str, Any]:
    """Compare credentials (mirrors ``credentials compare``)."""
    with chained_ctx(job) as (ctx, _, workdir, params):

        async def _main() -> Any:
            coordinator = _credential_coordinator(ctx)
            return await coordinator.compare_and_verify_credentials(
                report_path=str(workdir / "reports" / "credential-comparison.md")
            )

        result: dict[str, Any] = asyncio.run(_main())
    result["report"] = "reports/credential-comparison.md"
    result.setdefault("message", "Credential comparison complete")
    result.setdefault("artifacts", _artifacts(workdir, "reports"))
    return result


def run_credential_migrate(job: JobRecord) -> dict[str, Any]:
    """Migrate credentials (+ org/c Volumes as deps, mirrors ``credentials migrate``)."""
    with chained_ctx(job) as (ctx, _, workdir, params):

        async def _main() -> Any:
            coordinator = _credential_coordinator(ctx)
            if params.get("dry_run"):
                coordinator.config.dry_run = True
            comparison = await coordinator.compare_and_verify_credentials(
                report_path=str(workdir / "reports" / "credential-comparison.md")
            )
            if comparison.get("missing_count", 0) == 0:
                return {
                    "status": "no_action_needed",
                    "comparison": _relativize(comparison, workdir),
                }
            result = await coordinator.migrate_all(
                only_phases=["organizations", "credentials"],
                generate_report=True,
                report_dir=str(workdir / "reports"),
            )
            result["comparison"] = comparison
            return result

        result: dict[str, Any] = asyncio.run(_main())
        result = cast(dict[str, Any], _relativize(result, workdir))
        # No-action branch wins over the generic default (setdefault never
        # overwrites): check the specific status first so a no-op run
        # reports "No credential action needed", not "complete".
        if result.get("status") == "no_action_needed":
            result.setdefault("message", "No credential action needed")
        else:
            result.setdefault("message", "Credential migration complete")
        result.setdefault("artifacts", _artifacts(workdir, "reports"))
        return result
