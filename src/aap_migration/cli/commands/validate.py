"""Post-migration validation command.

Generates an HTML validation report comparing source exports against
migration database state, optionally fetching live data from AAP 2.6.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from aap_migration.cli.context import MigrationContext
from aap_migration.cli.decorators import handle_errors, pass_context, requires_config
from aap_migration.cli.utils import echo_info, echo_success, echo_warning
from aap_migration.validate.report import write_validation_report
from aap_migration.validate.runner import run_validation


@click.command(name="validate")
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    help="Output directory for report files (default: reports/)",
)
@click.option(
    "--live",
    is_flag=True,
    default=False,
    help="Fetch live field data from AAP 2.6 target API for full comparison",
)
@click.option(
    "--resource-type",
    "-r",
    type=str,
    help="Validate specific resource type only",
)
@pass_context
@requires_config
@handle_errors
def validate(
    ctx: MigrationContext,
    output_dir: str | None,
    live: bool,
    resource_type: str | None,
) -> None:
    """Post-migration validation report.

    Compares source exports against migration database state and generates
    a self-contained HTML report with field-level comparison.

    Default mode uses the migration database to confirm import status.
    With --live, fetches actual stored values from the AAP 2.6 target API
    for full field-level source vs target comparison.

    \b
    Examples:
        aap-bridge validate
        aap-bridge validate --live
        aap-bridge validate --live -r credentials
        aap-bridge validate -o /tmp/reports
    """
    output = Path(output_dir or ctx.config.paths.report_dir)
    target_client = None

    if live:
        echo_info("Validation mode: live (fetching from AAP 2.6 API)")
        target_client = ctx.target_client
    else:
        echo_info("Validation mode: database (import status only)")

    if resource_type:
        echo_info(f"Resource type filter: {resource_type}")

    result, field_data = asyncio.run(
        run_validation(
            config=ctx.config,
            migration_state=ctx.migration_state,
            target_client=target_client,
            live=live,
            resource_type=resource_type,
        )
    )

    json_path, html_path = write_validation_report(
        result, str(output), field_data=field_data,
    )

    echo_success(f"Validation report: {html_path}")

    total_source = sum(t.t1_counts.source for t in result.per_type)
    total_target = sum(t.t1_counts.target for t in result.per_type)
    total_missing = result.executive_summary.total_missing_on_target
    total_unexplained = sum(t.t1_counts.unexplained for t in result.per_type)

    echo_info(f"  Types: {len(result.per_type)}")
    echo_info(f"  Source objects: {total_source:,}")
    echo_info(f"  Target objects: {total_target:,}")
    echo_info(f"  Missing: {total_missing:,}")
    if total_unexplained > 0:
        echo_warning(f"  Unexplained: {total_unexplained:,}")
    echo_info(f"  Verdict: {result.executive_summary.verdict}")

    if not live:
        echo_info("")
        echo_info("  Field-level comparison not available in DB-only mode.")
        echo_info("  Run with --live for source vs target field comparison.")
