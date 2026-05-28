"""Health check CLI command."""

import asyncio
from pathlib import Path

import click
import structlog

from aap_migration.client.aap_client import AAPClient
from aap_migration.health import HealthChecker
from aap_migration.health.reporters import HTMLReporter, JSONReporter

logger = structlog.get_logger(__name__)


@click.command(name="health-check")
@click.option(
    "--source-url",
    required=True,
    help="Source AAP URL (e.g., https://aap24.example.com)",
    envvar="AAP_SOURCE_URL",
)
@click.option(
    "--source-token",
    required=True,
    help="Source AAP API token",
    envvar="AAP_SOURCE_TOKEN",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file path (default: health-report.html)",
    default="health-report.html",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["html", "json"], case_sensitive=False),
    default="html",
    help="Output format (html or json)",
)
@click.option(
    "--checks",
    "-c",
    multiple=True,
    help="Specific checks to run (default: all). Available: pending_deletion, duplicates, orphaned_references",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit with code 1 if critical issues found",
)
@click.option(
    "--no-verify-ssl",
    is_flag=True,
    help="Disable SSL certificate verification",
)
@click.pass_obj
def health_check_cmd(
    obj,
    source_url: str,
    source_token: str,
    output: Path,
    output_format: str,
    checks: tuple[str, ...],
    strict: bool,
    no_verify_ssl: bool,
):
    """Run pre-migration health checks on source AAP.

    This command analyzes the source AAP environment to identify data quality
    issues that could cause migration failures. It checks for:

    \b
    - Resources pending deletion
    - Duplicate resources (same name in same scope)
    - Orphaned references (broken foreign keys)

    Example usage:

    \b
        # Run all checks with HTML output
        aap-bridge health-check --source-url https://aap24.example.com --source-token $TOKEN

    \b
        # Run specific checks with JSON output
        aap-bridge health-check --checks pending_deletion --checks duplicates --format json -o health.json

    \b
        # Strict mode (exit code 1 if critical issues found)
        aap-bridge health-check --strict
    """
    logger.info(
        "health_check_command_started",
        source_url=source_url,
        output=str(output),
        format=output_format,
        checks=checks if checks else "all",
    )

    # Run health check
    try:
        asyncio.run(
            _run_health_check(
                source_url=source_url,
                source_token=source_token,
                output_path=output,
                output_format=output_format,
                check_names=list(checks) if checks else None,
                strict=strict,
                verify_ssl=not no_verify_ssl,
            )
        )
    except Exception as e:
        logger.error("health_check_failed", error=str(e), exc_info=True)
        click.echo(f"❌ Health check failed: {str(e)}", err=True)
        raise click.Abort()


async def _run_health_check(
    source_url: str,
    source_token: str,
    output_path: Path,
    output_format: str,
    check_names: list[str] | None,
    strict: bool,
    verify_ssl: bool,
):
    """Run health check asynchronously.

    Args:
        source_url: Source AAP URL
        source_token: Source AAP API token
        output_path: Output file path
        output_format: Output format (html or json)
        check_names: Specific checks to run (None = all)
        strict: Exit with error if critical issues found
        verify_ssl: Whether to verify SSL certificates
    """
    # Create AAP client
    client = AAPClient(
        base_url=source_url,
        token=source_token,
        verify_ssl=verify_ssl,
    )

    # Create health checker
    checker = HealthChecker(client)

    # Run checks
    click.echo("🏥 Running AAP pre-migration health checks...")
    click.echo(f"Source: {source_url}")
    click.echo("")

    if check_names:
        click.echo(f"Running checks: {', '.join(check_names)}")
        report = await checker.run_checks(check_names)
    else:
        click.echo("Running all available checks...")
        report = await checker.run_all_checks()

    # Display summary
    click.echo("")
    click.echo("=" * 60)
    click.echo("SUMMARY")
    click.echo("=" * 60)
    click.echo(f"Total Checks:    {report.summary['total_checks']}")
    click.echo(f"Passed:          {report.summary['passed']} ✅")
    click.echo(f"Critical Issues: {report.summary['critical']} ❌")
    click.echo(f"Warnings:        {report.summary['warning']} ⚠️")
    click.echo(f"Info:            {report.summary['info']} ℹ️")
    click.echo("")
    click.echo(f"Migration Readiness: {report.migration_readiness:.1f}%")

    if report.is_migration_ready:
        click.echo("✅ Ready for migration (no critical issues)")
    else:
        click.echo("❌ Fix critical issues before migration")

    click.echo("=" * 60)
    click.echo("")

    # Generate report
    if output_format == "html":
        content = HTMLReporter.generate(report)
    else:  # json
        content = JSONReporter.generate(report)

    # Write to file
    output_path.write_text(content)
    click.echo(f"Report saved to: {output_path}")

    # Exit with error if strict mode and critical issues found
    if strict and report.has_critical_issues:
        click.echo("")
        click.echo("❌ Critical issues found - exiting with error code", err=True)
        raise click.Abort()

    click.echo("")
    click.echo("✅ Health check completed")

    # Close client
    await client.close()
