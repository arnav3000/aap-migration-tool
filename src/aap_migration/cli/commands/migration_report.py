"""
Migration report commands.

This module provides commands for generating migration reports showing
success, failures, and discrepancies between exported and imported resources.
"""

import json
from datetime import datetime
from pathlib import Path

import click

from aap_migration.cli.context import MigrationContext
from aap_migration.cli.decorators import handle_errors, pass_context, requires_config
from aap_migration.cli.utils import echo_error, echo_info, echo_success
from aap_migration.migration.database import get_session
from aap_migration.migration.models import MigrationProgress
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


@click.command(name="migration-report")
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file for report (default: logs/migration-report.md)",
)
@click.option(
    "--resource-type",
    "-r",
    type=str,
    help="Generate report for specific resource type only",
)
@pass_context
@requires_config
@handle_errors
def generate_migration_report(
    ctx: MigrationContext,
    output: str | None,
    resource_type: str | None,
) -> None:
    """Generate comprehensive migration report with failures and discrepancies.

    This command analyzes the migration state and generates a detailed report showing:
    - Resources exported from source
    - Resources transformed
    - Resources successfully imported to target
    - Resources that failed
    - Discrepancies between exported and imported counts

    Examples:

        # Generate full migration report
        aap-bridge migration-report

        # Generate report for specific resource type
        aap-bridge migration-report --resource-type credentials

        # Save to custom location
        aap-bridge migration-report --output /tmp/migration-report.md
    """
    echo_info("Generating migration report...")

    # Set default output path
    if not output:
        output = ctx.config.paths.report_dir + "/migration-report.md"

    # Ensure report directory exists
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        migration_state = ctx.migration_state

        # Get paths
        export_dir = Path(ctx.config.paths.export_dir)
        transform_dir = Path(ctx.config.paths.transform_dir)

        # Determine resource types to analyze
        if resource_type:
            resource_types = [resource_type]
        else:
            # Auto-detect from database (most reliable source)
            resource_types = []
            try:
                with get_session(migration_state.database_url) as session:
                    db_resource_types = (
                        session.query(MigrationProgress.resource_type)
                        .distinct()
                        .all()
                    )
                    resource_types = [rt[0] for rt in db_resource_types]
            except Exception as e:
                logger.warning(f"Failed to query database for resource types: {e}")
                # Fallback: detect from export subdirectories
                for dir_path in export_dir.iterdir():
                    if dir_path.is_dir():
                        resource_types.append(dir_path.name)

        # Collect statistics for each resource type
        report_data = []

        for rtype in resource_types:
            stats = _analyze_resource_type(
                rtype,
                export_dir,
                transform_dir,
                migration_state.database_url,
            )
            report_data.append(stats)

        # Generate markdown report
        report_content = _generate_markdown_report(report_data, ctx.migration_state.migration_id)

        # Write report to file
        output_path.write_text(report_content)

        echo_success(f"Migration report generated: {output}")

        # Print summary to console
        _print_summary(report_data)

    except Exception as e:
        echo_error(f"Failed to generate migration report: {e}")
        logger.error("Migration report generation failed", error=str(e), exc_info=True)
        raise click.ClickException(str(e)) from e


def _identify_missing_resources(
    resource_type: str,
    transform_dir: Path,
    database_url: str,
) -> list[dict]:
    """Identify which specific resources are missing (transformed but not imported).

    Args:
        resource_type: Type of resource
        transform_dir: Directory containing transformed files
        database_url: Database connection URL

    Returns:
        List of missing resource details
    """
    missing = []
    transformed_data = []

    # Load transformed resources (handle both flat and directory structure)
    transform_subdir = transform_dir / resource_type
    if transform_subdir.exists() and transform_subdir.is_dir():
        # Directory-based structure: xformed/{resource_type}/{resource_type}_*.json
        for batch_file in sorted(transform_subdir.glob(f"{resource_type}_*.json")):
            try:
                with open(batch_file) as f:
                    batch_data = json.load(f)
                    if isinstance(batch_data, list):
                        transformed_data.extend(batch_data)
                    else:
                        transformed_data.append(batch_data)
            except Exception as e:
                logger.warning(f"Failed to read transform batch file {batch_file}: {e}")
    else:
        # Fallback: flat file structure: xformed/{resource_type}.json
        transform_file = transform_dir / f"{resource_type}.json"
        if not transform_file.exists():
            return missing

        try:
            with open(transform_file) as f:
                batch_data = json.load(f)
                if isinstance(batch_data, list):
                    transformed_data = batch_data
                else:
                    transformed_data = [batch_data]
        except Exception as e:
            logger.warning(f"Failed to read transform file {transform_file}: {e}")
            return missing

    if not transformed_data:
        return missing

    # Get completed source IDs from database
    try:
        with get_session(database_url) as session:
            completed_records = (
                session.query(MigrationProgress.source_id)
                .filter_by(resource_type=resource_type, status="completed")
                .all()
            )
            completed_ids = {record.source_id for record in completed_records}
    except Exception as e:
        logger.warning(f"Failed to query database for {resource_type}: {e}")
        return missing

    # Find resources that were transformed but not completed
    for resource in transformed_data:
        source_id = resource.get("id")
        if source_id and source_id not in completed_ids:
            missing.append({
                "source_id": source_id,
                "name": resource.get("name", "N/A"),
                "type": resource.get("type") or resource.get("credential_type"),
            })

    return missing


def _analyze_resource_type(
    resource_type: str,
    export_dir: Path,
    transform_dir: Path,
    database_url: str,
) -> dict:
    """Analyze a single resource type and collect statistics."""
    stats = {
        "resource_type": resource_type,
        "exported_count": 0,
        "transformed_count": 0,
        "completed_count": 0,
        "failed_count": 0,
        "in_progress_count": 0,
        "pending_count": 0,
        "skipped_count": 0,
        "failed_resources": [],
        "missing_resources": [],
    }

    # Count exported resources (handle both flat and directory structure)
    exported_data = []

    # Try directory-based structure first: exports/{resource_type}/{resource_type}_*.json
    export_subdir = export_dir / resource_type
    if export_subdir.exists() and export_subdir.is_dir():
        for batch_file in sorted(export_subdir.glob(f"{resource_type}_*.json")):
            try:
                with open(batch_file) as f:
                    batch_data = json.load(f)
                    if isinstance(batch_data, list):
                        exported_data.extend(batch_data)
                    else:
                        exported_data.append(batch_data)
            except Exception as e:
                logger.warning(f"Failed to read export batch file {batch_file}: {e}")
    else:
        # Fallback: try flat file structure: exports/{resource_type}.json
        export_file = export_dir / f"{resource_type}.json"
        if export_file.exists():
            try:
                with open(export_file) as f:
                    batch_data = json.load(f)
                    if isinstance(batch_data, list):
                        exported_data = batch_data
                    else:
                        exported_data = [batch_data]
            except Exception as e:
                logger.warning(f"Failed to read export file {export_file}: {e}")

    stats["exported_count"] = len(exported_data)

    # Count transformed resources (handle both flat and directory structure)
    transformed_data = []

    # Try directory-based structure first: xformed/{resource_type}/{resource_type}_*.json
    transform_subdir = transform_dir / resource_type
    if transform_subdir.exists() and transform_subdir.is_dir():
        for batch_file in sorted(transform_subdir.glob(f"{resource_type}_*.json")):
            try:
                with open(batch_file) as f:
                    batch_data = json.load(f)
                    if isinstance(batch_data, list):
                        transformed_data.extend(batch_data)
                    else:
                        transformed_data.append(batch_data)
            except Exception as e:
                logger.warning(f"Failed to read transform batch file {batch_file}: {e}")
    else:
        # Fallback: try flat file structure: xformed/{resource_type}.json
        transform_file = transform_dir / f"{resource_type}.json"
        if transform_file.exists():
            try:
                with open(transform_file) as f:
                    batch_data = json.load(f)
                    if isinstance(batch_data, list):
                        transformed_data = batch_data
                    else:
                        transformed_data = [batch_data]
            except Exception as e:
                logger.warning(f"Failed to read transform file {transform_file}: {e}")

    stats["transformed_count"] = len(transformed_data)

    # Query database for migration progress
    try:
        with get_session(database_url) as session:
            # Count by status
            progress_records = (
                session.query(MigrationProgress)
                .filter_by(resource_type=resource_type)
                .all()
            )

            for record in progress_records:
                if record.status == "completed":
                    stats["completed_count"] += 1
                elif record.status == "failed":
                    stats["failed_count"] += 1
                    stats["failed_resources"].append({
                        "source_id": record.source_id,
                        "source_name": record.source_name,
                        "error": record.error_message,
                        "phase": record.phase,
                    })
                elif record.status == "in_progress":
                    stats["in_progress_count"] += 1
                elif record.status == "pending":
                    stats["pending_count"] += 1
                elif record.status == "skipped":
                    stats["skipped_count"] += 1

    except Exception as e:
        logger.warning(f"Failed to query database for {resource_type}: {e}")

    # Calculate discrepancy (resources that are neither completed nor failed)
    stats["discrepancy"] = stats["transformed_count"] - (
        stats["completed_count"] + stats["failed_count"]
    )

    # Identify specific missing resources if there's a discrepancy
    if stats["discrepancy"] > 0:
        stats["missing_resources"] = _identify_missing_resources(
            resource_type,
            transform_dir,
            database_url,
        )

    return stats


def _generate_markdown_report(report_data: list[dict], migration_id: str) -> str:
    """Generate markdown-formatted migration report."""
    lines = [
        "# AAP Migration Report",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Migration ID:** {migration_id}",
        "",
        "---",
        "",
        "## Summary",
        "",
    ]

    # Summary table
    lines.append("| Resource Type | Exported | Transformed | Imported | Failed | Discrepancy |")
    lines.append("|---------------|----------|-------------|----------|--------|-------------|")

    total_exported = 0
    total_transformed = 0
    total_imported = 0
    total_failed = 0
    total_discrepancy = 0

    for stats in report_data:
        rtype = stats["resource_type"]
        exported = stats["exported_count"]
        transformed = stats["transformed_count"]
        imported = stats["completed_count"]
        failed = stats["failed_count"]
        discrepancy = stats["discrepancy"]

        # Format discrepancy with warning emoji if non-zero
        discrepancy_str = f"**{discrepancy}** ⚠️" if discrepancy != 0 else str(discrepancy)
        failed_str = f"**{failed}** ❌" if failed > 0 else str(failed)

        lines.append(
            f"| {rtype} | {exported} | {transformed} | {imported} | {failed_str} | {discrepancy_str} |"
        )

        total_exported += exported
        total_transformed += transformed
        total_imported += imported
        total_failed += failed
        total_discrepancy += discrepancy

    # Totals row
    total_discrepancy_str = f"**{total_discrepancy}**" if total_discrepancy != 0 else str(total_discrepancy)
    total_failed_str = f"**{total_failed}**" if total_failed > 0 else str(total_failed)

    lines.append(
        f"| **TOTAL** | **{total_exported}** | **{total_transformed}** | **{total_imported}** | {total_failed_str} | {total_discrepancy_str} |"
    )

    lines.append("")
    lines.append("---")
    lines.append("")

    # SECURITY FIX: Add workflow-specific correlation section
    # Show relationship between workflow_job_templates and workflow_nodes
    workflow_stats = next((s for s in report_data if s["resource_type"] == "workflow_job_templates"), None)
    node_stats = next((s for s in report_data if s["resource_type"] == "workflow_nodes"), None)

    if workflow_stats and node_stats:
        lines.append("## Workflow Job Templates - Node Import Status")
        lines.append("")
        lines.append("Workflow job templates consist of multiple workflow nodes. This section shows the correlation:")
        lines.append("")
        lines.append(f"- **Workflows imported:** {workflow_stats['completed_count']}")
        lines.append(f"- **Workflow nodes imported:** {node_stats['completed_count']}")
        lines.append(f"- **Workflow nodes failed:** {node_stats['failed_count']}")
        lines.append("")

        # Warning if nodes failed
        if node_stats['failed_count'] > 0:
            lines.append("⚠️ **WARNING:** Some workflow nodes failed to import!")
            lines.append("")
            lines.append("**Impact:**")
            lines.append("- Workflows may be incomplete or broken")
            lines.append("- Workflows may fail when executed in target AAP")
            lines.append("- Review failed workflow_nodes below for details")
            lines.append("")
            lines.append("**Recommendation:**")
            lines.append("- Ensure all job templates are successfully imported")
            lines.append("- Re-run workflow import after fixing job template issues")
            lines.append("- Verify workflows in target AAP UI before executing")
            lines.append("")

        # Warning if workflows failed
        if workflow_stats['failed_count'] > 0:
            lines.append("⚠️ **WARNING:** Some workflows failed to import!")
            lines.append("")
            lines.append(f"- **Workflows failed:** {workflow_stats['failed_count']}")
            lines.append("")
            lines.append("**Common causes:**")
            lines.append("- Missing job template dependencies (nodes couldn't be created)")
            lines.append("- Missing organization or inventory references")
            lines.append("- Invalid workflow configuration")
            lines.append("")

        lines.append("---")
        lines.append("")

    # Detailed sections for failures
    for stats in report_data:
        if stats["failed_count"] > 0 or stats["discrepancy"] != 0:
            lines.append(f"## {stats['resource_type']} - Issues")
            lines.append("")

            if stats["failed_count"] > 0:
                lines.append(f"### Failed Resources ({stats['failed_count']})")
                lines.append("")
                lines.append("| Source ID | Name | Phase | Error |")
                lines.append("|-----------|------|-------|-------|")

                for failed in stats["failed_resources"]:
                    source_id = failed["source_id"]
                    name = failed["source_name"] or "N/A"
                    phase = failed["phase"] or "N/A"
                    error = failed["error"] or "Unknown error"
                    # Escape pipe characters in error messages for markdown tables
                    error = error.replace("|", "\\|")
                    lines.append(f"| {source_id} | {name} | {phase} | {error} |")

                lines.append("")

            if stats["discrepancy"] > 0:
                lines.append(f"### Missing Resources (Discrepancy: {stats['discrepancy']})")
                lines.append("")
                lines.append(f"**Transformed:** {stats['transformed_count']}  ")
                lines.append(f"**Imported:** {stats['completed_count']}  ")
                lines.append(f"**Missing:** {stats['discrepancy']}")
                lines.append("")

                # Show list of specific missing resources
                if stats["missing_resources"]:
                    lines.append(f"#### Specific Missing Resources ({len(stats['missing_resources'])})")
                    lines.append("")
                    lines.append("| Source ID | Name | Type |")
                    lines.append("|-----------|------|------|")

                    for missing in stats["missing_resources"]:
                        source_id = missing["source_id"]
                        name = missing["name"]
                        res_type = missing.get("type", "N/A")
                        lines.append(f"| {source_id} | {name} | {res_type} |")

                    lines.append("")
                    lines.append("**These resources were transformed but not found in the database as completed.**")
                    lines.append("")

                lines.append("**Possible causes:**")
                lines.append("- Resources failed validation during import (check Failed Resources section)")
                lines.append("- Resources were skipped due to conflicts (already existed in target)")
                lines.append("- Resources failed dependency resolution")
                lines.append("- Check `logs/migration.log` for detailed error messages")
                lines.append("")

            lines.append("---")
            lines.append("")

    # Success message if everything is clean
    if total_failed == 0 and total_discrepancy == 0:
        lines.append("## ✅ Migration Completed Successfully")
        lines.append("")
        lines.append(f"All {total_imported} resources were imported successfully with no failures or discrepancies.")
        lines.append("")

    return "\n".join(lines)


def _print_summary(report_data: list[dict]) -> None:
    """Print summary to console."""
    click.echo()
    click.echo("=" * 80)
    click.echo("MIGRATION SUMMARY")
    click.echo("=" * 80)

    for stats in report_data:
        rtype = stats["resource_type"]
        discrepancy = stats["discrepancy"]
        failed = stats["failed_count"]

        # Color code based on status
        if failed > 0:
            status = click.style("FAILED", fg="red", bold=True)
        elif discrepancy > 0:
            status = click.style("WARNING", fg="yellow", bold=True)
        else:
            status = click.style("OK", fg="green")

        click.echo(
            f"{rtype:30s} | Exported: {stats['exported_count']:5d} | "
            f"Imported: {stats['completed_count']:5d} | "
            f"Failed: {failed:4d} | Discrepancy: {discrepancy:4d} | {status}"
        )

    click.echo("=" * 80)
    click.echo()
