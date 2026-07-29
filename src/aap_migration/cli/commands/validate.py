"""Post-migration validation command.

Generates an HTML validation report comparing source exports against
the live AAP target (with migration DB gap explanations) or database-only mode.
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
    help=(
        "Compare exports/ against the live AAP target API by object name; "
        "uses the migration DB to explain import gaps for unmatched objects"
    ),
)
@click.option(
    "--resource-type",
    "-r",
    type=str,
    help="Validate specific resource type only",
)
@click.option(
    "--skip-hosts",
    is_flag=True,
    default=False,
    help="Skip hosts (T1–T4 host checks and live host listing)",
)
@pass_context
@requires_config
@handle_errors
def validate(
    ctx: MigrationContext,
    output_dir: str | None,
    live: bool,
    resource_type: str | None,
    skip_hosts: bool,
) -> None:
    """Post-migration validation report.

    Default mode uses the migration database to confirm import status.
    With --live, compares exports/ to objects currently on the AAP target
    by identity (name / org / parent), including field-level deltas, and
    uses the migration DB to classify unmatched objects as explained
    (failed/skipped) or unexplained gaps.

    \b
    Examples:
        aap-bridge validate
        aap-bridge validate --live
        aap-bridge validate --live --skip-hosts
        aap-bridge validate --live -r credentials
        aap-bridge validate -o /tmp/reports
        # With -r, writes validation_report_<resource>.html/.json
    """
    if skip_hosts and resource_type == "hosts":
        raise click.ClickException("--skip-hosts conflicts with -r hosts")

    output = Path(output_dir or ctx.config.paths.report_dir)
    target_client = None
    migration_state = ctx.migration_state

    if live:
        echo_info(
            "Validation mode: live (exports vs AAP target; DB explains gaps)"
        )
        target_client = ctx.target_client
    else:
        echo_info("Validation mode: database (import status only)")

    if resource_type:
        echo_info(f"Resource type filter: {resource_type}")
    if skip_hosts:
        echo_info("Skipping hosts validation")

    result, field_data = asyncio.run(
        run_validation(
            config=ctx.config,
            migration_state=migration_state,
            target_client=target_client,
            live=live,
            resource_type=resource_type,
            skip_hosts=skip_hosts,
        )
    )

    json_filename = "validation_report.json"
    html_filename = "validation_report.html"
    if resource_type:
        # Safe single-segment suffix for filenames (e.g. credentials → validation_report_credentials.html)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in resource_type.strip())
        safe = safe.strip("_") or "resource"
        json_filename = f"validation_report_{safe}.json"
        html_filename = f"validation_report_{safe}.html"

    json_path, html_path = write_validation_report(
        result,
        str(output),
        json_filename=json_filename,
        html_filename=html_filename,
        field_data=field_data,
    )

    echo_success(f"Validation report: {html_path}")

    total_source = sum(t.t1_counts.source for t in result.per_type)
    total_target = sum(t.t1_counts.target for t in result.per_type)
    total_missing = result.executive_summary.total_missing_on_target
    total_unexplained = sum(t.t1_counts.unexplained for t in result.per_type)
    total_field_mm = result.executive_summary.total_field_mismatches

    echo_info(f"  Types: {len(result.per_type)}")
    echo_info(f"  Source objects: {total_source:,}")
    echo_info(f"  Target objects: {total_target:,}")
    echo_info(f"  Missing: {total_missing:,}")
    total_explained = sum(
        t.t1_counts.explained_failures + t.t1_counts.explained_skips
        for t in result.per_type
    )
    if total_explained > 0:
        echo_info(f"  Explained gaps: {total_explained:,}")
    if live:
        echo_info(f"  Field mismatches: {total_field_mm:,}")
        extra = result.executive_summary.total_extra_on_target
        if extra:
            echo_info(f"  Extra on target: {extra:,}")
    if total_unexplained > 0:
        echo_warning(f"  Unexplained: {total_unexplained:,}")
    echo_info(f"  Verdict: {result.executive_summary.verdict}")

    if not live:
        echo_info("")
        echo_info("  Field-level comparison not available in DB-only mode.")
        echo_info("  Run with --live for source vs target field comparison.")
