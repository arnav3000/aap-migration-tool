"""
Migration report commands.

This module provides commands for generating migration reports showing
success, failures, and discrepancies between exported and imported resources.
"""

import json
from datetime import datetime
from pathlib import Path

import click
from sqlalchemy import text

from aap_migration.cli.context import MigrationContext
from aap_migration.cli.decorators import handle_errors, pass_context, requires_config
from aap_migration.cli.utils import echo_error, echo_info, echo_success
from aap_migration.migration.database import get_session
from aap_migration.migration.models import MigrationProgress
from aap_migration.reporting.org_mapper import OrganizationMapper
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
@click.option(
    "--by-organization",
    is_flag=True,
    default=False,
    help="Generate organization-scoped failure report (groups failures by owning organization)",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["markdown", "csv", "html"], case_sensitive=False),
    default="markdown",
    help="Output format for organization report (default: markdown)",
)
@pass_context
@requires_config
@handle_errors
def generate_migration_report(
    ctx: MigrationContext,
    output: str | None,
    resource_type: str | None,
    by_organization: bool,
    output_format: str,
) -> None:
    """Generate comprehensive migration report with failures and discrepancies.

    This command analyzes the migration state and generates a detailed report showing:
    - Resources exported from source
    - Resources transformed
    - Resources successfully imported to target
    - Resources that failed
    - Discrepancies between exported and imported counts

    With --by-organization flag:
    - Groups failed/skipped resources by owning organization
    - Shows organization-level statistics
    - Maps resources to organizations using exported metadata

    Examples:

        # Generate full migration report
        aap-bridge migration-report

        # Generate report for specific resource type
        aap-bridge migration-report --resource-type credentials

        # Save to custom location
        aap-bridge migration-report --output /tmp/migration-report.md

        # Generate organization-scoped failure report
        aap-bridge migration-report --by-organization

        # Organization report in CSV format
        aap-bridge migration-report --by-organization --format csv

        # Organization report in HTML format (styled, browser-viewable)
        aap-bridge migration-report --by-organization --format html

        # Organization report for specific resource type
        aap-bridge migration-report --by-organization --resource-type credentials
    """
    # Branch to organization-scoped report if requested
    if by_organization:
        return _generate_organization_report(
            ctx=ctx,
            output=output,
            resource_type=resource_type,
            output_format=output_format,
        )

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
        report_content = _generate_markdown_report(report_data, ctx.migration_state)

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


def _format_workflow_nodes_failures(failed_resources: list[dict], migration_state) -> list[str]:
    """Format workflow node failures grouped by parent workflow.

    Args:
        failed_resources: List of failed workflow nodes
        migration_state: Migration state for database queries

    Returns:
        List of formatted lines for the report
    """
    import re
    from collections import defaultdict

    lines = []
    lines.append(f"### Failed Workflow Nodes ({len(failed_resources)})")
    lines.append("")
    lines.append("Workflow nodes are grouped by their parent workflow for better readability:")
    lines.append("")

    # Group nodes by parent workflow
    grouped_by_workflow = defaultdict(list)

    # Query database for workflow node data to get parent workflow IDs
    with get_session(migration_state.database_url) as session:
        for failed in failed_resources:
            source_id = failed["source_id"]

            # Query the export data for this workflow node to get parent workflow info
            # We need the workflow_job_template field from the node data
            workflow_id = None
            workflow_name = None

            # Parse the error message to extract job template source ID
            error = failed["error"] or ""
            jt_source_id = None
            jt_name = None

            # Extract job template source_id from error message
            # Format: "Referenced job template (source_id=33) was not successfully imported"
            match = re.search(r'source_id=(\d+)', error)
            if match:
                jt_source_id = int(match.group(1))

                # Look up job template name
                jt_record = (
                    session.query(MigrationProgress)
                    .filter_by(resource_type="job_templates", source_id=jt_source_id)
                    .first()
                )
                if jt_record:
                    jt_name = jt_record.source_name
                    jt_status = jt_record.status

            # Try to find parent workflow from export data
            # The workflow node should have workflow_job_template field in the exported data
            # We'll need to read from export files or query additional metadata
            # For now, use a simplified grouping

            # Create entry
            entry = {
                "source_id": source_id,
                "jt_source_id": jt_source_id,
                "jt_name": jt_name or f"Unknown (source ID: {jt_source_id})" if jt_source_id else "Unknown",
                "jt_status": jt_status if jt_source_id else None,
                "error": error,
            }

            # For now, group all under a generic key since we don't have easy access to parent workflow
            # In a more complete implementation, we'd parse export data or store this in the database
            grouped_by_workflow["workflow_nodes"].append(entry)

    # Format output grouped by workflow
    for workflow_key, nodes in grouped_by_workflow.items():
        lines.append(f"**All Workflow Nodes ({len(nodes)} failed):**")
        lines.append("")

        for node in nodes:
            jt_status_info = ""
            if node["jt_status"] == "skipped":
                jt_status_info = " (skipped - already exists in target)"
            elif node["jt_status"] == "failed":
                jt_status_info = " (failed to import)"
            elif node["jt_status"] == "completed":
                jt_status_info = " (successfully imported)"
            elif node["jt_status"] is None:
                jt_status_info = " (not found in migration)"

            lines.append(
                f"- Node {node['source_id']}: "
                f"References Job Template **'{node['jt_name']}'** (source ID: {node['jt_source_id']}){jt_status_info}"
            )

        lines.append("")

    lines.append("**Resolution:**")
    lines.append("- Ensure all referenced job templates are successfully imported first")
    lines.append("- Re-run workflow import after job template issues are resolved")
    lines.append("- Skipped job templates indicate duplicates already exist in target AAP")
    lines.append("")

    return lines


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
        # Phase-specific counts for accurate discrepancy calculation
        "import_skipped_count": 0,
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
                        stats["import_skipped_count"] += 1

    except Exception as e:
        logger.warning(f"Failed to query database for {resource_type}: {e}")

    # Calculate discrepancy (resources that are neither completed, failed, nor skipped)
    # Use import_skipped_count (not total skipped_count) because transform-phase skips
    # are NOT in the transformed files, so they shouldn't reduce the discrepancy.
    # Include in_progress_count since those resources are being processed.
    stats["discrepancy"] = stats["transformed_count"] - (
        stats["completed_count"]
        + stats["failed_count"]
        + stats["import_skipped_count"]
        + stats["in_progress_count"]
    )

    # Identify specific missing resources if there's a discrepancy
    if stats["discrepancy"] > 0:
        stats["missing_resources"] = _identify_missing_resources(
            resource_type,
            transform_dir,
            database_url,
        )

    return stats


def _generate_markdown_report(report_data: list[dict], migration_state) -> str:
    """Generate markdown-formatted migration report."""
    lines = [
        "# AAP Migration Report",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Migration ID:** {migration_state.migration_id}",
        "",
        "---",
        "",
        "## Summary",
        "",
    ]

    # Summary table
    lines.append("| Resource Type | Exported | Transformed | Imported | Failed | Skipped | In Progress | Discrepancy |")
    lines.append("|---------------|----------|-------------|----------|--------|---------|-------------|-------------|")

    total_exported = 0
    total_transformed = 0
    total_imported = 0
    total_failed = 0
    total_skipped = 0
    total_in_progress = 0
    total_discrepancy = 0

    for stats in report_data:
        rtype = stats["resource_type"]
        exported = stats["exported_count"]
        transformed = stats["transformed_count"]
        imported = stats["completed_count"]
        failed = stats["failed_count"]
        skipped = stats["skipped_count"]
        in_progress = stats["in_progress_count"]
        discrepancy = stats["discrepancy"]

        # Format discrepancy with warning emoji if non-zero
        discrepancy_str = f"**{discrepancy}** ⚠️" if discrepancy != 0 else str(discrepancy)
        failed_str = f"**{failed}** ❌" if failed > 0 else str(failed)
        skipped_str = f"**{skipped}** ⏭️" if skipped > 0 else str(skipped)
        in_progress_str = f"**{in_progress}** ⏳" if in_progress > 0 else str(in_progress)

        lines.append(
            f"| {rtype} | {exported} | {transformed} | {imported} | {failed_str} | {skipped_str} | {in_progress_str} | {discrepancy_str} |"
        )

        total_exported += exported
        total_transformed += transformed
        total_imported += imported
        total_failed += failed
        total_skipped += skipped
        total_in_progress += in_progress
        total_discrepancy += discrepancy

    # Totals row
    total_discrepancy_str = f"**{total_discrepancy}**" if total_discrepancy != 0 else str(total_discrepancy)
    total_failed_str = f"**{total_failed}**" if total_failed > 0 else str(total_failed)
    total_skipped_str = f"**{total_skipped}**" if total_skipped > 0 else str(total_skipped)
    total_in_progress_str = f"**{total_in_progress}**" if total_in_progress > 0 else str(total_in_progress)

    lines.append(
        f"| **TOTAL** | **{total_exported}** | **{total_transformed}** | **{total_imported}** | {total_failed_str} | {total_skipped_str} | {total_in_progress_str} | {total_discrepancy_str} |"
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
                # Special formatting for workflow_nodes - group by parent workflow
                if stats["resource_type"] == "workflow_nodes":
                    lines.extend(_format_workflow_nodes_failures(stats["failed_resources"], migration_state))
                else:
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

            # Add warnings section for successfully imported resources with warnings
            if stats["completed_count"] > 0:
                with get_session(migration_state.database_url) as session:
                    resources_with_warnings = (
                        session.query(MigrationProgress)
                        .filter(
                            MigrationProgress.resource_type == stats["resource_type"],
                            MigrationProgress.status == "completed",
                            MigrationProgress.error_message.isnot(None),
                            MigrationProgress.error_message.like("WARNING:%"),
                        )
                        .all()
                    )

                if resources_with_warnings:
                    lines.append(f"### Warnings ({len(resources_with_warnings)})")
                    lines.append("")
                    lines.append("These resources were successfully imported but have warnings (e.g., incomplete notification associations):")
                    lines.append("")
                    lines.append("| Source ID | Name | Warning |")
                    lines.append("|-----------|------|---------|")

                    for resource in resources_with_warnings:
                        source_id = resource.source_id
                        name = resource.source_name or "N/A"
                        warning = resource.error_message or "No warning message"
                        # Extract WARNING text (remove "WARNING: " prefix if present)
                        if warning.startswith("WARNING: "):
                            warning = warning[9:]  # Remove "WARNING: " prefix
                        # Escape pipe characters for markdown tables
                        warning = warning.replace("|", "\\|")
                        # Truncate long warnings
                        if len(warning) > 150:
                            warning = warning[:147] + "..."
                        lines.append(f"| {source_id} | {name} | {warning} |")

                    lines.append("")
                    lines.append("**Note:**")
                    lines.append("- These resources are functional but may have incomplete configurations")
                    lines.append("- Review warnings and manually complete missing associations if needed")
                    lines.append("- Common warnings: notification templates not migrated, credentials missing")
                    lines.append("")

            # Special check for credentials: detect duplicate target_id mappings
            # This indicates the bug where multiple source credentials were mapped to same target
            if stats["resource_type"] == "credentials" and stats["completed_count"] > 0:
                with get_session(migration_state.database_url) as session:
                    # Find target_ids with multiple source mappings
                    duplicate_mappings_query = text("""
                        SELECT target_id, COUNT(*) as mapping_count, GROUP_CONCAT(source_id) as source_ids
                        FROM id_mappings
                        WHERE resource_type = 'credentials'
                        GROUP BY target_id
                        HAVING mapping_count > 1
                        ORDER BY mapping_count DESC
                    """)
                    result = session.execute(duplicate_mappings_query)
                    duplicate_mappings = result.fetchall()

                if duplicate_mappings:
                    total_affected = sum(count - 1 for _, count, _ in duplicate_mappings)
                    lines.append(f"### ⚠️ CRITICAL: Duplicate Target Mappings Detected ({total_affected} credentials affected)")
                    lines.append("")
                    lines.append("**Problem:** Multiple source credentials were incorrectly mapped to the same target credential.")
                    lines.append(f"This means **{total_affected} credentials were NOT created** in target AAP.")
                    lines.append("")
                    lines.append("**Root Cause:** Credential lookup bug - query only checked 'name' field, ignoring 'organization' and 'credential_type'.")
                    lines.append("")
                    lines.append("**Impact:**")
                    lines.append("- Organizations are missing credentials they should have")
                    lines.append("- Job templates/workflows using these credentials may fail")
                    lines.append("- Multiple source credentials share same target credential (wrong organization)")
                    lines.append("")

                    lines.append("| Target ID | Source Count | Sample Source IDs | Organizations Affected |")
                    lines.append("|-----------|--------------|-------------------|------------------------|")

                    for target_id, mapping_count, source_ids_str in duplicate_mappings[:10]:  # Show top 10
                        source_ids = source_ids_str.split(",")[:5]  # Show first 5 source IDs
                        sample_ids = ", ".join(source_ids)
                        if mapping_count > 5:
                            sample_ids += f", ... (+{mapping_count - 5} more)"
                        orgs_affected = mapping_count - 1  # One org got it, others are missing
                        lines.append(f"| {target_id} | {mapping_count} | {sample_ids} | {orgs_affected} |")

                    if len(duplicate_mappings) > 10:
                        lines.append(f"| ... | ... | ... | ... |")
                        lines.append(f"| **({len(duplicate_mappings) - 10} more)** | | | |")

                    lines.append("")
                    lines.append("**Recommended Actions:**")
                    lines.append("1. **URGENT:** Apply the credential import bug fix (check with migration tool maintainer)")
                    lines.append("2. Re-run credential import to create missing credentials")
                    lines.append("3. Verify all organizations have their required credentials")
                    lines.append("4. Test job templates/workflows after credential fix")
                    lines.append("")
                    lines.append("**Technical Details:**")
                    lines.append("- See detailed list of affected credentials in separate analysis file")
                    lines.append("- Bug affects credentials with same name but different organizations")
                    lines.append("- Fix: Query by composite key (name, organization, credential_type)")
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


def _generate_organization_report(
    ctx: MigrationContext,
    output: str | None,
    resource_type: str | None,
    output_format: str,
) -> None:
    """Generate organization-scoped failure report.

    Args:
        ctx: Migration context
        output: Output file path
        resource_type: Optional resource type filter
        output_format: Output format (markdown or csv)
    """
    echo_info("Generating organization-scoped failure report...")

    # Set default output path based on format
    if not output:
        extension_map = {"markdown": "md", "csv": "csv", "html": "html"}
        extension = extension_map.get(output_format, "md")
        output = f"{ctx.config.paths.report_dir}/org-failures.{extension}"

    # Ensure report directory exists
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        migration_state = ctx.migration_state
        export_dir = Path(ctx.config.paths.export_dir)
        transform_dir = Path(ctx.config.paths.transform_dir)

        # Initialize organization mapper
        echo_info("Loading organization mappings...")
        org_mapper = OrganizationMapper(export_dir, transform_dir)

        # For HTML format, query ALL resources to show complete picture
        # For markdown/CSV, query only failures/skipped (current behavior)
        if output_format == "html":
            echo_info("Querying all resources for complete report...")
            all_resources = []

            with get_session(migration_state.database_url) as session:
                query = session.query(MigrationProgress).filter(
                    MigrationProgress.phase == "import"
                )

                # Filter by resource type if specified
                if resource_type:
                    query = query.filter(MigrationProgress.resource_type == resource_type)

                for record in query.all():
                    all_resources.append({
                        "resource_type": record.resource_type,
                        "source_id": record.source_id,
                        "source_name": record.source_name,
                        "status": record.status,
                        "error_message": record.error_message,
                        "phase": record.phase,
                    })

            echo_info(f"Found {len(all_resources)} total resources")

            # Build organization summary with ALL resources
            echo_info("Mapping resources to organizations...")
            org_summary = org_mapper.build_org_summary(all_resources)

            # Generate HTML with complete data
            report_content = _format_org_report_html(org_summary, migration_state)

        else:
            # For markdown/CSV: query only failed and skipped resources
            echo_info("Querying failed and skipped resources...")
            failures = []

            with get_session(migration_state.database_url) as session:
                query = session.query(MigrationProgress).filter(
                    MigrationProgress.status.in_(["failed", "skipped"])
                )

                # Filter by resource type if specified
                if resource_type:
                    query = query.filter(MigrationProgress.resource_type == resource_type)

                for record in query.all():
                    failures.append({
                        "resource_type": record.resource_type,
                        "source_id": record.source_id,
                        "source_name": record.source_name,
                        "status": record.status,
                        "error_message": record.error_message,
                        "phase": record.phase,
                    })

            if not failures:
                echo_success("No failures or skipped resources found!")
                return

            echo_info(f"Found {len(failures)} failed/skipped resources")

            # Build organization summary
            echo_info("Mapping resources to organizations...")
            org_summary = org_mapper.build_org_summary(failures)

            # Generate report
            if output_format == "markdown":
                report_content = _format_org_report_markdown(org_summary, migration_state)
            else:  # csv
                report_content = _format_org_report_csv(org_summary)

        # Write report
        output_path.write_text(report_content)
        echo_success(f"Organization report generated: {output}")

        # Print summary
        _print_org_summary(org_summary)

    except Exception as e:
        echo_error(f"Failed to generate organization report: {e}")
        logger.error("Organization report generation failed", error=str(e), exc_info=True)
        raise click.ClickException(str(e)) from e


def _format_org_report_markdown(org_summary: dict, migration_state) -> str:
    """Format organization summary as markdown."""
    lines = [
        "# AAP Migration - Organization Failure Report",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Migration ID:** {migration_state.migration_id}",
        "",
        "---",
        "",
        "## Summary by Organization",
        "",
    ]

    # Summary table
    lines.append("| Organization | Failed | Skipped | Total | Resource Types Affected |")
    lines.append("|--------------|--------|---------|-------|------------------------|")

    # Sort by total (descending)
    sorted_orgs = sorted(
        org_summary.items(),
        key=lambda x: x[1]["total"],
        reverse=True,
    )

    total_failed = 0
    total_skipped = 0
    total_all = 0

    for org_name, summary in sorted_orgs:
        failed = summary["failed"]
        skipped = summary["skipped"]
        total = summary["total"]
        resource_types = ", ".join(sorted(summary["resource_types"]))

        failed_str = f"**{failed}**" if failed > 0 else str(failed)
        skipped_str = f"**{skipped}**" if skipped > 0 else str(skipped)

        lines.append(f"| {org_name} | {failed_str} | {skipped_str} | {total} | {resource_types} |")

        total_failed += failed
        total_skipped += skipped
        total_all += total

    # Totals row
    lines.append(f"| **TOTAL** | **{total_failed}** | **{total_skipped}** | **{total_all}** | - |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Detailed sections per organization
    for org_name, summary in sorted_orgs:
        lines.append(f"## {org_name}")
        lines.append("")
        lines.append(f"**Statistics:**")
        lines.append(f"- Failed: {summary['failed']}")
        lines.append(f"- Skipped: {summary['skipped']}")
        lines.append(f"- Total: {summary['total']}")
        lines.append(f"- Resource Types: {', '.join(sorted(summary['resource_types']))}")
        lines.append("")

        # Group by resource type
        by_type = {}
        for resource in summary["resources"]:
            rtype = resource["resource_type"]
            if rtype not in by_type:
                by_type[rtype] = []
            by_type[rtype].append(resource)

        for rtype in sorted(by_type.keys()):
            resources = by_type[rtype]
            lines.append(f"### {rtype} ({len(resources)})")
            lines.append("")
            lines.append("| Source ID | Name | Status | Error/Reason |")
            lines.append("|-----------|------|--------|--------------|")

            for resource in resources:
                source_id = resource["source_id"]
                source_name = resource.get("source_name", "N/A")
                status = resource["status"]
                error = resource.get("error_message", "No error message")
                # Escape pipe characters
                error = error.replace("|", "\\|")
                # Truncate long errors
                if len(error) > 100:
                    error = error[:97] + "..."

                lines.append(f"| {source_id} | {source_name} | {status} | {error} |")

            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def _format_org_report_csv(org_summary: dict) -> str:
    """Format organization summary as CSV."""
    import csv
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        "Organization",
        "Resource Type",
        "Source ID",
        "Source Name",
        "Status",
        "Error/Reason",
    ])

    # Sort organizations by total (descending)
    sorted_orgs = sorted(
        org_summary.items(),
        key=lambda x: x[1]["total"],
        reverse=True,
    )

    # Write data
    for org_name, summary in sorted_orgs:
        for resource in summary["resources"]:
            writer.writerow([
                org_name,
                resource["resource_type"],
                resource["source_id"],
                resource.get("source_name", "N/A"),
                resource["status"],
                resource.get("error_message", "No error message"),
            ])

    return output.getvalue()


def _format_org_report_html(org_summary: dict, migration_state) -> str:
    """Format organization summary as interactive HTML with tabs and filtering.

    Creates a tabbed interface with:
    - Summary tab: All orgs with success rates
    - Failures tab: Failed + skipped resources
    - Successful tab: Completed resources
    - Complete tab: All resources

    Designed to handle 1000+ organizations and 2M+ objects efficiently by:
    - Loading data once as embedded JSON
    - Using JavaScript for client-side filtering
    - Only rendering visible content
    - Pagination for large result sets
    """
    import json
    from html import escape

    # Prepare data for JSON embedding
    json_data = {
        "metadata": {
            "generated": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "migration_id": str(migration_state.migration_id),
        },
        "organizations": {}
    }

    # Convert org_summary to JSON-friendly format
    # Now includes completed resources for success tracking
    for org_name, summary in org_summary.items():
        # Count by status
        completed = sum(1 for r in summary["resources"] if r["status"] == "completed")
        failed = sum(1 for r in summary["resources"] if r["status"] == "failed")
        skipped = sum(1 for r in summary["resources"] if r["status"] == "skipped")

        json_data["organizations"][org_name] = {
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "total": summary["total"],
            "resource_types": sorted(list(summary["resource_types"])),
            "resources": summary["resources"]
        }

    # Generate tabbed interactive HTML app
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AAP Migration - Organization Report</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            min-height: 100vh;
        }}
        .container {{
            max-width: 1600px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }}
        .header h1 {{ font-size: 2em; margin-bottom: 10px; }}
        .header .metadata {{ opacity: 0.9; font-size: 0.9em; }}
        .tabs {{
            display: flex;
            background: #f8f9fa;
            border-bottom: 3px solid #e9ecef;
            overflow-x: auto;
        }}
        .tab {{
            padding: 15px 30px;
            cursor: pointer;
            border: none;
            background: transparent;
            font-size: 1em;
            font-weight: 600;
            color: #6c757d;
            transition: all 0.3s;
            border-bottom: 3px solid transparent;
            margin-bottom: -3px;
            white-space: nowrap;
        }}
        .tab:hover {{ background: #e9ecef; color: #495057; }}
        .tab.active {{
            color: #667eea;
            border-bottom-color: #667eea;
            background: white;
        }}
        .controls {{
            background: #fff;
            padding: 20px 30px;
            border-bottom: 2px solid #e9ecef;
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
            align-items: center;
        }}
        .controls.hidden {{ display: none; }}
        .control-group {{
            display: flex;
            flex-direction: column;
            gap: 5px;
        }}
        .control-group label {{
            font-size: 0.85em;
            font-weight: 600;
            color: #495057;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        select, input[type="text"] {{
            padding: 10px 15px;
            border: 2px solid #dee2e6;
            border-radius: 6px;
            font-size: 14px;
            min-width: 200px;
            transition: all 0.3s;
        }}
        select:focus, input[type="text"]:focus {{
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }}
        .stats {{
            padding: 20px 30px;
            background: #fff;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 15px;
        }}
        .stat-card {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
        }}
        .stat-card.success {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
        .stat-card.failed {{ background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); }}
        .stat-card.skipped {{ background: linear-gradient(135deg, #ffecd2 0%, #fcb69f 100%); }}
        .stat-card .value {{ font-size: 2.5em; font-weight: bold; margin-bottom: 5px; }}
        .stat-card .label {{ font-size: 0.9em; opacity: 0.9; }}
        .content {{ padding: 30px; min-height: 400px; }}
        .content.hidden {{ display: none; }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
            font-size: 0.9em;
        }}
        th {{
            background: #667eea;
            color: white;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            position: sticky;
            top: 0;
        }}
        td {{
            padding: 10px 12px;
            border-bottom: 1px solid #e9ecef;
        }}
        tr:hover {{ background: #f8f9fa; }}
        tr.clickable {{ cursor: pointer; }}
        .status-completed {{
            background: #38ef7d;
            color: white;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.85em;
            font-weight: 600;
        }}
        .status-failed {{
            background: #f5576c;
            color: white;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.85em;
            font-weight: 600;
        }}
        .status-skipped {{
            background: #fcb69f;
            color: #333;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.85em;
            font-weight: 600;
        }}
        .success-rate {{
            font-weight: 600;
            padding: 4px 8px;
            border-radius: 4px;
        }}
        .success-rate.high {{ background: #d4edda; color: #155724; }}
        .success-rate.medium {{ background: #fff3cd; color: #856404; }}
        .success-rate.low {{ background: #f8d7da; color: #721c24; }}
        .pagination {{
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 10px;
            padding: 20px;
            margin-top: 20px;
        }}
        .pagination.hidden {{ display: none; }}
        .pagination button {{
            padding: 8px 16px;
            border: 2px solid #667eea;
            background: white;
            color: #667eea;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
            transition: all 0.3s;
        }}
        .pagination button:hover:not(:disabled) {{
            background: #667eea;
            color: white;
        }}
        .pagination button:disabled {{
            opacity: 0.3;
            cursor: not-allowed;
        }}
        .pagination .page-info {{
            padding: 0 15px;
            font-weight: 600;
            color: #495057;
        }}
        .resource-type-section {{
            margin-bottom: 30px;
        }}
        .resource-type-section h3 {{
            color: #495057;
            padding: 10px 0;
            border-bottom: 2px solid #e9ecef;
            margin-bottom: 15px;
        }}
        .error-cell {{
            max-width: 400px;
            word-wrap: break-word;
            font-size: 0.85em;
            color: #495057;
        }}
        .no-data {{
            text-align: center;
            padding: 60px 20px;
            color: #6c757d;
        }}
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>📊 AAP Migration - Organization Report</h1>
        <div class="metadata">
            Generated: {escape(json_data["metadata"]["generated"])} |
            Migration ID: {escape(json_data["metadata"]["migration_id"])}
        </div>
    </div>
    <div class="tabs">
        <button class="tab active" data-tab="summary">📊 Summary</button>
        <button class="tab" data-tab="failures">❌ Failures</button>
        <button class="tab" data-tab="successful">✅ Successful</button>
        <button class="tab" data-tab="complete">📋 Complete</button>
    </div>
    <div class="controls hidden" id="controls">
        <div class="control-group">
            <label for="orgSelect">Select Organization</label>
            <select id="orgSelect">
                <option value="">-- Select Organization --</option>
            </select>
        </div>
        <div class="control-group">
            <label for="resourceTypeFilter">Resource Type</label>
            <select id="resourceTypeFilter">
                <option value="">All Types</option>
            </select>
        </div>
        <div class="control-group">
            <label for="searchInput">Search</label>
            <input type="text" id="searchInput" placeholder="Search by name or ID...">
        </div>
    </div>
    <div class="stats" id="statsContainer"></div>
    <div class="content" id="summaryContent"></div>
    <div class="content hidden" id="failuresContent"></div>
    <div class="content hidden" id="successfulContent"></div>
    <div class="content hidden" id="completeContent"></div>
    <div class="pagination hidden" id="paginationContainer">
        <button id="prevPage">← Previous</button>
        <span class="page-info" id="pageInfo">Page 1 of 1</span>
        <button id="nextPage">Next →</button>
    </div>
</div>
<script>
const DATA = {json.dumps(json_data, indent=2)};
let currentTab = 'summary';
let currentOrg = null;
let currentPage = 1;
const itemsPerPage = 100;
let filteredData = [];

function init() {{
    populateOrgDropdown();
    renderSummaryTab();
    attachEventListeners();
}}

function populateOrgDropdown() {{
    const select = document.getElementById('orgSelect');
    const orgs = Object.keys(DATA.organizations).sort((a, b) => DATA.organizations[b].total - DATA.organizations[a].total);
    orgs.forEach(org => {{
        const option = document.createElement('option');
        option.value = org;
        const stats = DATA.organizations[org];
        const successRate = stats.total > 0 ? Math.round((stats.completed / stats.total) * 100) : 0;
        option.textContent = `${{org}} (Success: ${{successRate}}%, Total: ${{stats.total}})`;
        select.appendChild(option);
    }});
}}

function switchTab(tabName) {{
    currentTab = tabName;
    currentPage = 1;
    document.querySelectorAll('.tab').forEach(tab => {{
        tab.classList.toggle('active', tab.dataset.tab === tabName);
    }});
    document.querySelectorAll('.content').forEach(content => content.classList.add('hidden'));
    const controls = document.getElementById('controls');
    if (tabName === 'summary') {{
        controls.classList.add('hidden');
        document.getElementById('summaryContent').classList.remove('hidden');
        renderSummaryTab();
    }} else {{
        controls.classList.remove('hidden');
        document.getElementById(`${{tabName}}Content`).classList.remove('hidden');
        if (!currentOrg && Object.keys(DATA.organizations).length > 0) {{
            const firstOrg = Object.keys(DATA.organizations).sort((a, b) => DATA.organizations[b].total - DATA.organizations[a].total)[0];
            document.getElementById('orgSelect').value = firstOrg;
            currentOrg = firstOrg;
            populateResourceTypeFilter();
        }}
        renderDetailTab();
    }}
}}

function renderSummaryTab() {{
    const orgs = Object.entries(DATA.organizations);
    const totalOrgs = orgs.length;
    const totalCompleted = orgs.reduce((sum, [_, data]) => sum + data.completed, 0);
    const totalFailed = orgs.reduce((sum, [_, data]) => sum + data.failed, 0);
    const totalSkipped = orgs.reduce((sum, [_, data]) => sum + data.skipped, 0);
    const totalAll = orgs.reduce((sum, [_, data]) => sum + data.total, 0);
    const overallSuccessRate = totalAll > 0 ? Math.round((totalCompleted / totalAll) * 100) : 0;
    document.getElementById('statsContainer').innerHTML = `
        <div class="stat-card">
            <div class="value">${{totalOrgs}}</div>
            <div class="label">Organizations</div>
        </div>
        <div class="stat-card success">
            <div class="value">${{totalCompleted}}</div>
            <div class="label">Successful</div>
        </div>
        <div class="stat-card failed">
            <div class="value">${{totalFailed}}</div>
            <div class="label">Failed</div>
        </div>
        <div class="stat-card skipped">
            <div class="value">${{totalSkipped}}</div>
            <div class="label">Skipped</div>
        </div>
        <div class="stat-card">
            <div class="value">${{overallSuccessRate}}%</div>
            <div class="label">Success Rate</div>
        </div>
    `;
    const sortedOrgs = orgs.sort((a, b) => b[1].total - a[1].total);
    let tableHtml = '<table><thead><tr><th>Organization</th><th>Total</th><th>Successful</th><th>Failed</th><th>Skipped</th><th>Success Rate</th><th>Resource Types</th></tr></thead><tbody>';
    sortedOrgs.forEach(([orgName, stats]) => {{
        const successRate = stats.total > 0 ? Math.round((stats.completed / stats.total) * 100) : 0;
        let rateClass = successRate < 50 ? 'low' : (successRate < 80 ? 'medium' : 'high');
        tableHtml += `<tr class="clickable" onclick="goToOrg('${{orgName.replace(/'/g, "\\'")}}')" title="Click to view details">
            <td><strong>${{escapeHtml(orgName)}}</strong></td>
            <td>${{stats.total}}</td>
            <td><span class="status-completed">${{stats.completed}}</span></td>
            <td><span class="status-failed">${{stats.failed}}</span></td>
            <td><span class="status-skipped">${{stats.skipped}}</span></td>
            <td><span class="success-rate ${{rateClass}}">${{successRate}}%</span></td>
            <td style="font-size: 0.85em;">${{stats.resource_types.join(', ')}}</td>
        </tr>`;
    }});
    tableHtml += '</tbody></table>';
    document.getElementById('summaryContent').innerHTML = tableHtml;
    document.getElementById('paginationContainer').classList.add('hidden');
}}

function goToOrg(orgName) {{
    document.getElementById('orgSelect').value = orgName;
    currentOrg = orgName;
    populateResourceTypeFilter();
    switchTab('failures');
}}

function populateResourceTypeFilter() {{
    if (!currentOrg) return;
    const orgData = DATA.organizations[currentOrg];
    const resourceTypeSelect = document.getElementById('resourceTypeFilter');
    resourceTypeSelect.innerHTML = '<option value="">All Types</option>';
    orgData.resource_types.forEach(type => {{
        const option = document.createElement('option');
        option.value = type;
        option.textContent = type;
        resourceTypeSelect.appendChild(option);
    }});
}}

function renderDetailTab() {{
    if (!currentOrg) {{
        const contentId = `${{currentTab}}Content`;
        document.getElementById(contentId).innerHTML = '<div class="no-data"><h3>Please select an organization from the dropdown above</h3></div>';
        document.getElementById('statsContainer').innerHTML = '';
        document.getElementById('paginationContainer').classList.add('hidden');
        return;
    }}
    const orgData = DATA.organizations[currentOrg];
    const resourceTypeFilter = document.getElementById('resourceTypeFilter').value;
    const searchTerm = document.getElementById('searchInput').value.toLowerCase();
    let statusFilter = [];
    if (currentTab === 'failures') statusFilter = ['failed', 'skipped'];
    else if (currentTab === 'successful') statusFilter = ['completed'];
    else statusFilter = ['completed', 'failed', 'skipped'];
    filteredData = orgData.resources.filter(resource => {{
        if (!statusFilter.includes(resource.status)) return false;
        if (resourceTypeFilter && resource.resource_type !== resourceTypeFilter) return false;
        if (searchTerm) {{
            const matchName = resource.source_name && resource.source_name.toLowerCase().includes(searchTerm);
            const matchId = resource.source_id && resource.source_id.toString().includes(searchTerm);
            const matchError = resource.error_message && resource.error_message.toLowerCase().includes(searchTerm);
            if (!matchName && !matchId && !matchError) return false;
        }}
        return true;
    }});
    const completed = filteredData.filter(r => r.status === 'completed').length;
    const failed = filteredData.filter(r => r.status === 'failed').length;
    const skipped = filteredData.filter(r => r.status === 'skipped').length;
    const successRate = filteredData.length > 0 ? Math.round((completed / filteredData.length) * 100) : 0;
    document.getElementById('statsContainer').innerHTML = `
        <div class="stat-card"><div class="value" style="font-size: 1.8em;">${{escapeHtml(currentOrg)}}</div><div class="label">Selected Organization</div></div>
        <div class="stat-card success"><div class="value">${{completed}}</div><div class="label">Successful</div></div>
        <div class="stat-card failed"><div class="value">${{failed}}</div><div class="label">Failed</div></div>
        <div class="stat-card skipped"><div class="value">${{skipped}}</div><div class="label">Skipped</div></div>
        <div class="stat-card"><div class="value">${{successRate}}%</div><div class="label">Success Rate</div></div>
    `;
    renderPage();
}}

function renderPage() {{
    const start = (currentPage - 1) * itemsPerPage;
    const end = start + itemsPerPage;
    const pageData = filteredData.slice(start, end);
    const contentId = `${{currentTab}}Content`;
    if (pageData.length === 0) {{
        document.getElementById(contentId).innerHTML = '<div class="no-data"><p>No resources match the current filters</p></div>';
        document.getElementById('paginationContainer').classList.add('hidden');
        return;
    }}
    const byType = {{}};
    pageData.forEach(resource => {{
        if (!byType[resource.resource_type]) byType[resource.resource_type] = [];
        byType[resource.resource_type].push(resource);
    }});
    let html = '';
    Object.keys(byType).sort().forEach(resourceType => {{
        const resources = byType[resourceType];
        html += `<div class="resource-type-section"><h3>${{resourceType}} (${{resources.length}})</h3><table><thead><tr><th>Source ID</th><th>Name</th><th>Status</th>${{currentTab !== 'successful' ? '<th>Error/Reason</th>' : ''}}</tr></thead><tbody>`;
        resources.forEach(resource => {{
            const statusMap = {{'completed': 'status-completed', 'failed': 'status-failed', 'skipped': 'status-skipped'}};
            const statusClass = statusMap[resource.status];
            const error = resource.error_message || (resource.status === 'completed' ? 'Success' : 'No message');
            const truncatedError = error.length > 150 ? error.substring(0, 150) + '...' : error;
            html += `<tr title="${{escapeHtml(error)}}"><td>${{resource.source_id}}</td><td>${{escapeHtml(resource.source_name || 'N/A')}}</td><td><span class="${{statusClass}}">${{resource.status}}</span></td>${{currentTab !== 'successful' ? `<td class="error-cell">${{escapeHtml(truncatedError)}}</td>` : ''}}</tr>`;
        }});
        html += '</tbody></table></div>';
    }});
    document.getElementById(contentId).innerHTML = html;
    const totalPages = Math.ceil(filteredData.length / itemsPerPage);
    if (totalPages > 1) {{
        document.getElementById('paginationContainer').classList.remove('hidden');
        document.getElementById('pageInfo').textContent = `Page ${{currentPage}} of ${{totalPages}} (Showing ${{start + 1}}-${{Math.min(end, filteredData.length)}} of ${{filteredData.length}})`;
        document.getElementById('prevPage').disabled = currentPage === 1;
        document.getElementById('nextPage').disabled = currentPage === totalPages;
    }} else {{
        document.getElementById('paginationContainer').classList.add('hidden');
    }}
}}

function attachEventListeners() {{
    document.querySelectorAll('.tab').forEach(tab => {{
        tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    }});
    document.getElementById('orgSelect').addEventListener('change', () => {{
        currentOrg = document.getElementById('orgSelect').value;
        currentPage = 1;
        if (currentOrg) {{
            populateResourceTypeFilter();
            renderDetailTab();
        }} else {{
            const contentId = `${{currentTab}}Content`;
            document.getElementById(contentId).innerHTML = '<div class="no-data"><h3>Please select an organization</h3></div>';
            document.getElementById('statsContainer').innerHTML = '';
            document.getElementById('paginationContainer').classList.add('hidden');
        }}
    }});
    document.getElementById('resourceTypeFilter').addEventListener('change', () => {{ currentPage = 1; renderDetailTab(); }});
    document.getElementById('searchInput').addEventListener('input', () => {{ currentPage = 1; renderDetailTab(); }});
    document.getElementById('prevPage').addEventListener('click', () => {{ if (currentPage > 1) {{ currentPage--; renderPage(); }} }});
    document.getElementById('nextPage').addEventListener('click', () => {{
        const totalPages = Math.ceil(filteredData.length / itemsPerPage);
        if (currentPage < totalPages) {{ currentPage++; renderPage(); }}
    }});
}}

function escapeHtml(text) {{
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}}

init();
</script>
</body>
</html>"""

    return html


def _print_org_summary(org_summary: dict) -> None:
    """Print organization summary to console."""
    click.echo()
    click.echo("=" * 100)
    click.echo("ORGANIZATION FAILURE SUMMARY")
    click.echo("=" * 100)

    # Sort by total (descending)
    sorted_orgs = sorted(
        org_summary.items(),
        key=lambda x: x[1]["total"],
        reverse=True,
    )

    for org_name, summary in sorted_orgs:
        failed = summary["failed"]
        skipped = summary["skipped"]
        total = summary["total"]

        # Color code based on status
        if failed > 0:
            status = click.style("HAS FAILURES", fg="red", bold=True)
        elif skipped > 0:
            status = click.style("HAS SKIPPED", fg="cyan", bold=True)
        else:
            status = click.style("OK", fg="green")

        click.echo(
            f"{org_name:40s} | Failed: {failed:4d} | Skipped: {skipped:4d} | Total: {total:4d} | {status}"
        )

    click.echo("=" * 100)
    click.echo()


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
        in_progress = stats["in_progress_count"]

        # Color code based on status
        if failed > 0:
            status = click.style("FAILED", fg="red", bold=True)
        elif discrepancy > 0:
            status = click.style("WARNING", fg="yellow", bold=True)
        elif in_progress > 0:
            status = click.style("IN PROGRESS", fg="yellow")
        elif skipped > 0:
            status = click.style("SKIPPED", fg="cyan", bold=True)
        else:
            status = click.style("OK", fg="green")

        click.echo(
            f"{rtype:30s} | Exported: {stats['exported_count']:5d} | "
            f"Imported: {stats['completed_count']:5d} | "
            f"Failed: {failed:4d} | Skipped: {skipped:4d} | In Progress: {in_progress:4d} | Discrepancy: {discrepancy:4d} | {status}"
        )

    click.echo("=" * 100)
    click.echo()
