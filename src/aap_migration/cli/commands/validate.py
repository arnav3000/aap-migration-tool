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
from aap_migration.validate.report import (
    resolve_validate_report_dir,
)
from aap_migration.validate.org_report import write_org_scoped_validation_reports
from aap_migration.validate.runner import parse_orgs_arg, run_validation


@click.command(name="validate")
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    help="Base report directory (default: reports/). Writes under validate/<date>/…",
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
@click.option(
    "--orgs",
    type=str,
    default=None,
    help=(
        "Comma-separated organization names to scope validation. "
        "Skips pure globals (users, credential_types, instance_groups, "
        "instances, settings) and the auditor check. Live mode uses API "
        "org/parent filters (Plan B); FK name maps for globals are still "
        "loaded for field compare. Multi-org writes a combined report under "
        "…/multi/ plus per-org reports under …/<org-name>/ (same path as a "
        "single --orgs run)"
    ),
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
    orgs: str | None,
) -> None:
    """Post-migration validation report.

    Default mode uses the migration database to confirm import status.
    With --live, compares exports/ to objects currently on the AAP target
    by identity (name / org / parent), including field-level deltas, and
    uses the migration DB to classify unmatched objects as explained
    (failed/skipped) or unexplained gaps.

    Reports are written under::

        <output-dir>/validate/<YYYY-MM-DD>/<live|database>/[org|multi]/[type]/report.html

    With multiple --orgs::

        …/multi/report.html          # combined
        …/<OrgA>/report.html         # per org (same path as --orgs OrgA)
        …/<OrgB>/report.html

    Same calendar day overwrites the matching path.

    \b
    Examples:
        aap-bridge validate
        aap-bridge validate --live
        aap-bridge validate --live --skip-hosts
        aap-bridge validate --live -r credentials
        aap-bridge validate --orgs Team-alan
        aap-bridge validate --live --orgs Team-alan
        aap-bridge validate --orgs "OrgA, OrgB"
        aap-bridge validate -o /tmp/reports
    """
    if skip_hosts and resource_type == "hosts":
        raise click.ClickException("--skip-hosts conflicts with -r hosts")

    organizations = parse_orgs_arg(orgs)

    base_output = Path(output_dir or ctx.config.paths.report_dir)
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
    if organizations:
        echo_info(f"Organization scope: {', '.join(organizations)}")
        if live:
            echo_info("Auditor check skipped (org-scoped run)")
        if len(organizations) > 1:
            echo_info(
                "Writing combined report under multi/ plus per-org reports"
            )
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
            organizations=organizations,
        )
    )

    report_dir = resolve_validate_report_dir(
        base_output,
        live=live,
        organizations=organizations,
        resource_type=resource_type,
    )
    echo_info(f"Report directory: {report_dir}")

    written = write_org_scoped_validation_reports(
        result,
        base_dir=base_output,
        live=live,
        organizations=organizations or [],
        resource_type=resource_type,
        field_data=field_data,
    )

    for label, _json_path, html_path in written:
        if label == "combined":
            echo_success(f"Validation report: {html_path}")
        else:
            echo_success(f"  Org report ({label}): {html_path}")

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
