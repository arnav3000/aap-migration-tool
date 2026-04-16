"""CLI command for analyzing cross-organization dependencies."""

from __future__ import annotations

import asyncio
import sys

import click

from aap_migration.analysis.dependency_analyzer import CrossOrgDependencyAnalyzer
from aap_migration.analysis.reports import (
    format_detailed_report,
    format_summary_report,
)
from aap_migration.cli.context import MigrationContext
from aap_migration.cli.decorators import with_migration_context
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


@click.command(name="analyze-dependencies")
@click.option(
    "-o",
    "--organization",
    multiple=True,
    help="Organization name(s) to analyze. Can specify multiple times.",
)
@click.option(
    "--all",
    "analyze_all",
    is_flag=True,
    help="Analyze all organizations in source AAP.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show detailed analysis for each organization (Format B).",
)
@with_migration_context
def analyze_dependencies_cmd(
    ctx: MigrationContext,
    organization: tuple[str, ...],
    analyze_all: bool,
    verbose: bool,
):
    """Analyze cross-organization dependencies for migration planning.

    This tool helps you understand which organizations depend on others,
    allowing you to plan the correct migration order.

    Examples:

        # Analyze specific organization
        aap-bridge analyze-dependencies -o "Default"

        # Analyze multiple organizations
        aap-bridge analyze-dependencies -o "Default" -o "Engineering"

        # Analyze all organizations
        aap-bridge analyze-dependencies --all

        # Detailed view for single org
        aap-bridge analyze-dependencies -o "Default" --verbose
    """
    asyncio.run(run_analysis(ctx, organization, analyze_all, verbose))


async def run_analysis(
    ctx: MigrationContext,
    organizations: tuple[str, ...],
    analyze_all: bool,
    verbose: bool,
):
    """Run dependency analysis."""
    try:
        # Validate input
        if not analyze_all and not organizations:
            click.echo("Error: Must specify --all or -o ORGANIZATION")
            click.echo("Run 'aap-bridge analyze-dependencies --help' for usage")
            sys.exit(1)

        if analyze_all and organizations:
            click.echo("Error: Cannot use --all with -o ORGANIZATION")
            sys.exit(1)

        # Create analyzer
        analyzer = CrossOrgDependencyAnalyzer(ctx.source_client)

        if analyze_all:
            # Analyze all organizations
            click.echo("ℹ Analyzing all organizations in source AAP...")
            click.echo("")

            global_report = await analyzer.analyze_all_organizations()

            # Print summary report
            click.echo(format_summary_report(global_report))

        elif len(organizations) == 1:
            # Single organization - show detailed report
            org_name = organizations[0]
            click.echo(f"ℹ Analyzing organization: {org_name}")
            click.echo("")

            org_report = await analyzer.analyze_organization(org_name)

            # Print detailed report
            click.echo(format_detailed_report(org_report))

        else:
            # Multiple organizations - show summary
            click.echo(f"ℹ Analyzing {len(organizations)} organizations...")
            click.echo("")

            org_reports = {}
            for org_name in organizations:
                org_report = await analyzer.analyze_organization(org_name)
                org_reports[org_name] = org_report

            # Build mini global report
            from datetime import datetime

            from aap_migration.analysis.dependency_graph import (
                group_into_phases,
                topological_sort,
            )

            independent = sorted([name for name, r in org_reports.items()
                                  if not r.has_cross_org_deps])
            dependent = sorted([name for name, r in org_reports.items()
                                if r.has_cross_org_deps])

            graph = {org: report.required_migrations_before
                     for org, report in org_reports.items()}
            migration_order = topological_sort(graph)
            migration_phases = group_into_phases(graph, migration_order)

            # Create global report for these orgs
            from aap_migration.analysis.dependency_analyzer import (
                GlobalDependencyReport,
            )

            global_report = GlobalDependencyReport(
                analysis_date=datetime.now(),
                source_url=str(ctx.source_client.base_url),
                total_organizations=len(organizations),
                analyzed_organizations=list(organizations),
                independent_orgs=independent,
                dependent_orgs=dependent,
                org_reports=org_reports,
                migration_order=migration_order,
                migration_phases=migration_phases,
            )

            # Print summary
            click.echo(format_summary_report(global_report))

            # If verbose, also print detailed for each org
            if verbose:
                click.echo("")
                click.echo("=" * 67)
                click.echo("DETAILED ANALYSIS")
                click.echo("=" * 67)
                click.echo("")
                for org_name in organizations:
                    click.echo(format_detailed_report(org_reports[org_name]))
                    click.echo("")

    except Exception as e:
        logger.error(
            "dependency_analysis_failed",
            error=str(e),
            message=f"Dependency analysis failed: {e}"
        )
        click.echo(f"✗ Analysis failed: {e}")
        sys.exit(1)
