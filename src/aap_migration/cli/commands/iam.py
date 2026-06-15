"""CLI commands for IAM analysis and permission migration.

Provides three subcommands:
  audit   — read-only scan of source AAP, exports permission matrix
  migrate — full migration of permissions to target AAP
  report  — regenerate HTML report from a previous JSON export
"""

from __future__ import annotations

import os
import sys

import click


def _echo(msg: str) -> None:
    click.echo(msg)


@click.group()
def iam() -> None:
    """IAM analysis and permission migration."""


@iam.command()
@click.option(
    "--output-dir",
    type=click.Path(),
    default="./iam_reports/",
    show_default=True,
    help="Directory for report output.",
)
@click.option(
    "--verify-ssl/--no-verify-ssl",
    default=True,
    show_default=True,
    help="Verify TLS certificates on API calls.",
)
@click.option(
    "--timeout",
    type=int,
    default=60,
    show_default=True,
    help="HTTP request timeout in seconds.",
)
@click.pass_context
def audit(
    ctx: click.Context,
    output_dir: str,
    verify_ssl: bool,
    timeout: int,
) -> None:
    """Read-only scan: export permission matrix and generate report.

    Connects to the SOURCE AAP only. No target URL or token required.

    Required env vars: SOURCE__URL, SOURCE__TOKEN
    """
    from aap_migration.iam.analyser import IAMAnalyser
    from aap_migration.iam.report import write_iam_report

    source_url = os.environ.get("SOURCE__URL", "")
    source_token = os.environ.get("SOURCE__TOKEN", "")

    if not source_url or not source_token:
        click.echo(
            "Error: SOURCE__URL and SOURCE__TOKEN environment variables required",
            err=True,
        )
        sys.exit(1)

    try:
        with IAMAnalyser(
            source_url=source_url,
            source_token=source_token,
            verify_ssl=verify_ssl,
            request_timeout=timeout,
            progress_callback=_echo,
        ) as analyser:
            result = analyser.audit()

        json_path, html_path = write_iam_report(
            result,
            output_dir,
            json_filename="iam_audit.json",
            html_filename="iam_audit.html",
        )

        s = result.stats
        click.echo("")
        click.echo("=" * 60)
        click.echo("  IAM AUDIT SUMMARY")
        click.echo("=" * 60)
        click.echo(f"  Resources scanned:      {s.resources_scanned}")
        click.echo(f"  Permissions found:      {s.permissions_found}")
        click.echo(f"  Deduplicated:           {s.permissions_deduplicated}")
        click.echo(f"  Team memberships:       {s.team_memberships_found}")
        click.echo(f"  System roles:           {s.system_roles_found}")
        click.echo(f"  Cross-org shares:       {s.cross_org_shares}")
        click.echo("")
        click.echo(f"  JSON report: {json_path}")
        click.echo(f"  HTML report: {html_path}")
        click.echo("=" * 60)

    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@iam.command()
@click.option(
    "--output-dir",
    type=click.Path(),
    default="./iam_reports/",
    show_default=True,
    help="Directory for report output.",
)
@click.option(
    "--verify-ssl/--no-verify-ssl",
    default=True,
    show_default=True,
    help="Verify TLS certificates on API calls.",
)
@click.option(
    "--state-db",
    type=click.Path(),
    default=None,
    help="Path to migration state DB for ID mappings.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Resolve target IDs and show what WOULD be assigned.",
)
@click.option(
    "--timeout",
    type=int,
    default=60,
    show_default=True,
    help="HTTP request timeout in seconds.",
)
@click.pass_context
def migrate(
    ctx: click.Context,
    output_dir: str,
    verify_ssl: bool,
    state_db: str | None,
    dry_run: bool,
    timeout: int,
) -> None:
    """Full migration: team memberships + resource permissions.

    Scans source AAP, then applies permissions to target AAP.

    Required env vars: SOURCE__URL, SOURCE__TOKEN, TARGET__URL, TARGET__TOKEN
    """
    from aap_migration.iam.analyser import IAMAnalyser
    from aap_migration.iam.report import write_iam_report

    source_url = os.environ.get("SOURCE__URL", "")
    source_token = os.environ.get("SOURCE__TOKEN", "")
    target_url = os.environ.get("TARGET__URL", "")
    target_token = os.environ.get("TARGET__TOKEN", "")

    if not source_url or not source_token:
        click.echo(
            "Error: SOURCE__URL and SOURCE__TOKEN environment variables required",
            err=True,
        )
        sys.exit(1)

    if not target_url or not target_token:
        click.echo(
            "Error: TARGET__URL and TARGET__TOKEN environment variables required",
            err=True,
        )
        click.echo(
            "Hint: use 'aap-bridge iam audit' for read-only scan", err=True
        )
        sys.exit(1)

    if state_db is None:
        state_db = os.environ.get("MIGRATION_STATE_DB_PATH")

    label = "DRY-RUN" if dry_run else "MIGRATION"
    prefix = "iam_dry_run" if dry_run else "iam_migration"

    try:
        with IAMAnalyser(
            source_url=source_url,
            source_token=source_token,
            target_url=target_url,
            target_token=target_token,
            state_db_path=state_db,
            verify_ssl=verify_ssl,
            request_timeout=timeout,
            progress_callback=_echo,
        ) as analyser:
            result = analyser.migrate(dry_run=dry_run)

        json_path, html_path = write_iam_report(
            result,
            output_dir,
            json_filename=f"{prefix}.json",
            html_filename=f"{prefix}.html",
        )

        s = result.stats
        click.echo("")
        click.echo("=" * 60)
        click.echo(f"  IAM {label} SUMMARY")
        click.echo("=" * 60)
        click.echo(f"  Resources scanned:      {s.resources_scanned}")
        click.echo(f"  Permissions found:      {s.permissions_found}")
        click.echo(f"  Permissions migrated:   {s.permissions_migrated}")
        click.echo(f"  Permissions failed:     {s.permissions_failed}")
        if s.permissions_found > 0:
            rate = (s.permissions_migrated / s.permissions_found) * 100
            click.echo(f"  Success rate:           {rate:.1f}%")
        click.echo(f"  Team members migrated:  {s.team_memberships_migrated}")
        click.echo(f"  Team members failed:    {s.team_memberships_failed}")
        click.echo("")
        click.echo(f"  JSON report: {json_path}")
        click.echo(f"  HTML report: {html_path}")
        click.echo("=" * 60)

    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@iam.command()
@click.argument("json_path", type=click.Path(exists=True))
@click.option(
    "--output-dir",
    type=click.Path(),
    default=None,
    help="Output directory (defaults to same dir as JSON file).",
)
def report(json_path: str, output_dir: str | None) -> None:
    """Regenerate HTML report from a previous JSON export."""
    from aap_migration.iam.report import (
        generate_iam_html_report,
        load_audit_result_from_json,
    )

    try:
        result = load_audit_result_from_json(json_path)

        if output_dir is None:
            output_dir = os.path.dirname(os.path.abspath(json_path))

        html_content = generate_iam_html_report(result)
        html_filename = os.path.splitext(os.path.basename(json_path))[0] + ".html"
        html_path = os.path.join(output_dir, html_filename)

        os.makedirs(output_dir, mode=0o700, exist_ok=True)
        fd = os.open(
            html_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(fd, "w") as fh:
            fh.write(html_content)

        click.echo(f"HTML report generated: {html_path}")

    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
