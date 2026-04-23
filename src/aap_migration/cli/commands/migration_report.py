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
        "skipped_resources": [],
        "missing_resources": [],
        # Phase-specific tracking
        "export_failed": [],
        "transform_skipped": [],
        "import_failed": [],
        "import_skipped": [],
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
                # FIX: Count "imported" only if status="completed" AND phase="import"
                # This prevents counting resources from previous runs or transform phase
                # Database structure:
                #   - status="completed" + phase="import" = successfully imported to target AAP
                #   - status="skipped" + phase="import" = skipped during import (duplicate)
                #   - status="failed" + phase="import" = failed during import
                if record.status == "completed" and record.phase == "import":
                    stats["completed_count"] += 1

                if record.status == "failed":
                    stats["failed_count"] += 1
                    failure_info = {
                        "source_id": record.source_id,
                        "source_name": record.source_name,
                        "error": record.error_message,
                        "phase": record.phase,
                    }
                    stats["failed_resources"].append(failure_info)

                    # Separate by phase for detailed reporting
                    if record.phase == "export":
                        stats["export_failed"].append(failure_info)
                    elif record.phase == "import":
                        stats["import_failed"].append(failure_info)

                elif record.status == "in_progress":
                    stats["in_progress_count"] += 1
                elif record.status == "pending":
                    stats["pending_count"] += 1
                elif record.status == "skipped":
                    stats["skipped_count"] += 1
                    skip_info = {
                        "source_id": record.source_id,
                        "source_name": record.source_name,
                        "reason": record.error_message,
                        "phase": record.phase,
                    }
                    stats["skipped_resources"].append(skip_info)

                    # Separate by phase for detailed reporting
                    if record.phase == "transform":
                        stats["transform_skipped"].append(skip_info)
                    elif record.phase == "import":
                        stats["import_skipped"].append(skip_info)

    except Exception as e:
        logger.warning(f"Failed to query database for {resource_type}: {e}")

    # Calculate discrepancy (resources that are neither completed, failed, nor skipped)
    stats["discrepancy"] = stats["transformed_count"] - (
        stats["completed_count"] + stats["failed_count"] + stats["skipped_count"]
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
    lines.append("| Resource Type | Exported | Transformed | Imported | Failed | Skipped | Discrepancy |")
    lines.append("|---------------|----------|-------------|----------|--------|---------|-------------|")

    total_exported = 0
    total_transformed = 0
    total_imported = 0
    total_failed = 0
    total_skipped = 0
    total_discrepancy = 0

    for stats in report_data:
        rtype = stats["resource_type"]
        exported = stats["exported_count"]
        transformed = stats["transformed_count"]
        imported = stats["completed_count"]
        failed = stats["failed_count"]
        skipped = stats["skipped_count"]
        discrepancy = stats["discrepancy"]

        # Format discrepancy with warning emoji if non-zero
        discrepancy_str = f"**{discrepancy}** ⚠️" if discrepancy != 0 else str(discrepancy)
        failed_str = f"**{failed}** ❌" if failed > 0 else str(failed)
        skipped_str = f"**{skipped}** ⏭️" if skipped > 0 else str(skipped)

        lines.append(
            f"| {rtype} | {exported} | {transformed} | {imported} | {failed_str} | {skipped_str} | {discrepancy_str} |"
        )

        total_exported += exported
        total_transformed += transformed
        total_imported += imported
        total_failed += failed
        total_skipped += skipped
        total_discrepancy += discrepancy

    # Totals row
    total_discrepancy_str = f"**{total_discrepancy}**" if total_discrepancy != 0 else str(total_discrepancy)
    total_failed_str = f"**{total_failed}**" if total_failed > 0 else str(total_failed)
    total_skipped_str = f"**{total_skipped}**" if total_skipped > 0 else str(total_skipped)

    lines.append(
        f"| **TOTAL** | **{total_exported}** | **{total_transformed}** | **{total_imported}** | {total_failed_str} | {total_skipped_str} | {total_discrepancy_str} |"
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

    # Phase-specific sections (Export, Transform issues)
    export_issues_found = any(len(s.get("export_failed", [])) > 0 for s in report_data)
    transform_issues_found = any(len(s.get("transform_skipped", [])) > 0 for s in report_data)

    if export_issues_found:
        lines.append("## Export Phase Issues")
        lines.append("")
        lines.append("The following resources failed to export from source AAP:")
        lines.append("")

        for stats in report_data:
            if len(stats.get("export_failed", [])) > 0:
                lines.append(f"### {stats['resource_type']} ({len(stats['export_failed'])} failed)")
                lines.append("")
                lines.append("| Source ID | Name | Error |")
                lines.append("|-----------|------|-------|")

                for failed in stats["export_failed"]:
                    source_id = failed["source_id"]
                    name = failed["source_name"] or "N/A"
                    error = failed["error"] or "Unknown error"
                    error = error.replace("|", "\\|")
                    lines.append(f"| {source_id} | {name} | {error} |")

                lines.append("")

        lines.append("**Impact:**")
        lines.append("- These resources are missing from exports/ directory")
        lines.append("- Cannot be transformed or imported")
        lines.append("")
        lines.append("**Recommended Actions:**")
        lines.append("- Verify source AAP connectivity and permissions")
        lines.append("- Check source AAP logs for API errors")
        lines.append("- Re-run export for affected resource types")
        lines.append("")
        lines.append("---")
        lines.append("")

    if transform_issues_found:
        lines.append("## Transform Phase Issues")
        lines.append("")
        lines.append("The following resources were skipped during transformation due to missing dependencies:")
        lines.append("")

        for stats in report_data:
            if len(stats.get("transform_skipped", [])) > 0:
                lines.append(f"### {stats['resource_type']} ({len(stats['transform_skipped'])} skipped)")
                lines.append("")
                lines.append("| Source ID | Name | Reason |")
                lines.append("|-----------|------|--------|")

                for skipped in stats["transform_skipped"]:
                    source_id = skipped["source_id"]
                    name = skipped["source_name"] or "N/A"
                    reason = skipped.get("reason", "No reason provided")
                    reason = reason.replace("|", "\\|")
                    lines.append(f"| {source_id} | {name} | {reason} |")

                lines.append("")

        lines.append("**Impact:**")
        lines.append("- These resources are in exports/ but not in xformed/ directory")
        lines.append("- Cannot be imported because dependencies are missing")
        lines.append("")
        lines.append("**Recommended Actions:**")
        lines.append("- Export and transform the missing dependencies first")
        lines.append("- Example: if a schedule references missing job_template:42, export job_templates")
        lines.append("- Re-run transform after dependencies are available")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Detailed sections for failures, skipped, and discrepancies
    for stats in report_data:
        if stats["failed_count"] > 0 or stats["skipped_count"] > 0 or stats["discrepancy"] != 0:
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

            if stats["skipped_count"] > 0:
                lines.append(f"### Skipped Resources ({stats['skipped_count']})")
                lines.append("")
                lines.append("These resources were intentionally skipped and require manual resolution:")
                lines.append("")
                lines.append("| Source ID | Name | Reason |")
                lines.append("|-----------|------|--------|")

                for skipped in stats["skipped_resources"]:
                    source_id = skipped["source_id"]
                    name = skipped["source_name"] or "N/A"
                    reason = skipped["reason"] or "No reason provided"
                    # Escape pipe characters in reason for markdown tables
                    reason = reason.replace("|", "\\|")
                    lines.append(f"| {source_id} | {name} | {reason} |")

                lines.append("")
                lines.append("**Action Required:**")
                lines.append("- Review each skipped resource and its reason")
                lines.append("- Follow the instructions in the reason column to resolve")
                lines.append("- Typically requires renaming duplicates in source AAP or manual creation in target AAP")
                lines.append("")

            if stats["discrepancy"] > 0:
                lines.append(f"### Missing Resources (Discrepancy: {stats['discrepancy']})")
                lines.append("")
                lines.append("**Pipeline Summary:**")
                lines.append(f"- **Exported:** {stats['exported_count']}")
                lines.append(f"- **Transformed:** {stats['transformed_count']}")
                lines.append(f"- **Imported:** {stats['completed_count']}")
                lines.append(f"- **Discrepancy:** {stats['discrepancy']}")
                lines.append("")

                # Calculate gaps at each phase
                export_transform_gap = stats['exported_count'] - stats['transformed_count']
                transform_import_gap = stats['transformed_count'] - stats['completed_count']

                lines.append("**Gap Analysis:**")
                if export_transform_gap > 0:
                    lines.append(f"- Export → Transform: **{export_transform_gap}** resources lost (check Transform Phase Issues above)")
                elif export_transform_gap < 0:
                    lines.append(f"- Export → Transform: {abs(export_transform_gap)} additional resources (possibly seeded)")
                else:
                    lines.append(f"- Export → Transform: ✅ No gap")

                if transform_import_gap > 0:
                    lines.append(f"- Transform → Import: **{transform_import_gap}** resources lost (check Failed/Skipped sections below)")
                elif transform_import_gap < 0:
                    lines.append(f"- Transform → Import: {abs(transform_import_gap)} additional resources (data inconsistency)")
                else:
                    lines.append(f"- Transform → Import: ✅ No gap")
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

                lines.append("**Recommended Actions:**")
                lines.append("1. Check Export Phase Issues section for export failures")
                lines.append("2. Check Transform Phase Issues section for transform skips")
                lines.append("3. Check Failed/Skipped sections for import issues")
                lines.append("4. Review `logs/migration.log` for detailed error messages")
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
    click.echo("=" * 100)
    click.echo("MIGRATION SUMMARY")
    click.echo("=" * 100)

    for stats in report_data:
        rtype = stats["resource_type"]
        discrepancy = stats["discrepancy"]
        failed = stats["failed_count"]
        skipped = stats["skipped_count"]

        # Color code based on status
        if failed > 0:
            status = click.style("FAILED", fg="red", bold=True)
        elif discrepancy > 0:
            status = click.style("WARNING", fg="yellow", bold=True)
        elif skipped > 0:
            status = click.style("SKIPPED", fg="cyan", bold=True)
        else:
            status = click.style("OK", fg="green")

        click.echo(
            f"{rtype:30s} | Exported: {stats['exported_count']:5d} | "
            f"Imported: {stats['completed_count']:5d} | "
            f"Failed: {failed:4d} | Skipped: {skipped:4d} | Discrepancy: {discrepancy:4d} | {status}"
        )

    click.echo("=" * 100)
    click.echo()
