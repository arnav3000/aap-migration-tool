"""Retry and resume commands for failed imports."""

import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from aap_migration.cli.context import MigrationContext
from aap_migration.cli.decorators import handle_errors, pass_context, requires_config
from aap_migration.cli.utils import (
    echo_error,
    echo_info,
    echo_success,
    echo_warning,
)
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


@click.group(name="retry")
def retry_group():
    """Retry failed imports and resume interrupted migrations."""
    pass


@retry_group.command(name="failed")
@click.option(
    "--resource-type",
    "-r",
    multiple=True,
    help="Resource type to retry (can be specified multiple times). If not specified, retries all failed resources.",
)
@click.option(
    "--input",
    "input_dir",
    type=click.Path(exists=True, path_type=Path),
    help="Input directory with transformed data (default: from config)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be retried without actually importing",
)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Skip confirmation prompt",
)
@pass_context
@requires_config
@handle_errors
def retry_failed(
    ctx: MigrationContext,
    resource_type: tuple,
    input_dir: Path | None,
    dry_run: bool,
    yes: bool,
) -> None:
    """Retry all failed resources.

    This command identifies all resources that failed during previous import
    attempts and retries importing them. Useful for recovering from transient
    errors or after fixing dependency issues.

    Examples:

        # Retry all failed resources
        aap-bridge retry failed

        # Retry only failed credentials
        aap-bridge retry failed -r credentials

        # See what would be retried
        aap-bridge retry failed --dry-run
    """
    console = Console()

    if input_dir is None:
        input_dir = Path(ctx.config.paths.transform_dir)

    # Get failed resources from migration state
    from aap_migration.migration.database import get_session
    from aap_migration.migration.models import MigrationProgress

    state = ctx.migration_state

    # Query failed resources using proper session
    with get_session(state.database_url) as session:
        query = session.query(
            MigrationProgress.resource_type,
            MigrationProgress.source_id,
            MigrationProgress.source_name,
            MigrationProgress.error_message
        ).filter(MigrationProgress.status == 'failed')

        if resource_type:
            query = query.filter(MigrationProgress.resource_type.in_(resource_type))

        query = query.order_by(MigrationProgress.resource_type, MigrationProgress.source_id)

        failed_resources = query.all()

    if not failed_resources:
        echo_success("No failed resources to retry!")
        return

    # Group by resource type
    grouped = {}
    for row in failed_resources:
        rtype = row[0]
        if rtype not in grouped:
            grouped[rtype] = []
        grouped[rtype].append({
            "source_id": row[1],
            "name": row[2],
            "error": row[3],
        })

    # Display summary
    console.print("\n[bold yellow]Failed Resources to Retry:[/bold yellow]\n")

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Resource Type", style="cyan", width=25)
    table.add_column("Failed Count", justify="right", width=15)
    table.add_column("Sample Resources", width=50)

    for rtype, resources in grouped.items():
        sample_names = [r["name"] for r in resources[:3] if r["name"]]
        sample_text = ", ".join(sample_names)
        if len(resources) > 3:
            sample_text += f" ... and {len(resources) - 3} more"

        table.add_row(
            rtype,
            str(len(resources)),
            sample_text if sample_text else "N/A",
        )

    console.print(table)
    console.print(f"\n[bold]Total Failed: {len(failed_resources)}[/bold]\n")

    if dry_run:
        echo_info("Dry run mode - no changes will be made")
        return

    # Confirm before proceeding
    if not yes:
        confirm = click.confirm("\nRetry these failed resources?", default=True)
        if not confirm:
            echo_info("Retry cancelled")
            return

    # Clear failed status to allow retry
    click.echo()
    echo_info("Clearing failed status to enable retry...")

    with get_session(state.database_url) as session:
        for rtype, resources in grouped.items():
            for resource in resources:
                # Reset status from 'failed' to NULL to allow re-import
                progress = (
                    session.query(MigrationProgress)
                    .filter_by(resource_type=rtype, source_id=resource["source_id"])
                    .first()
                )
                if progress:
                    progress.status = None
            session.commit()

    echo_success(f"Cleared {len(failed_resources)} failed resource statuses")

    # Now run import using the proven migrate command
    click.echo()
    echo_info("Starting import of previously failed resources...")
    echo_info("(Using proven migrate command to retry)")
    click.echo()

    # Build config path argument
    config_arg = []
    if ctx.config_path:
        config_arg = ["--config", str(ctx.config_path)]

    # Import each resource type that had failures using proven migrate command
    for rtype in grouped.keys():
        echo_info(f"Retrying {rtype}...")

        # Build command using the proven migrate command
        cmd = [
            sys.executable, "-m", "aap_migration.cli.main",
        ] + config_arg + [
            "migrate",
            "-r", rtype,
            "--skip-prep",
            "--phase", "all"
        ]

        try:
            # Run the proven migrate command
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=False,  # Show output in real-time
                text=True
            )

            if result.returncode == 0:
                echo_success(f"  ✓ {rtype} retry completed")
            else:
                echo_warning(f"  ⚠ {rtype} retry finished with errors")

        except Exception as e:
            echo_error(f"Failed to retry {rtype}: {e}")
            continue

    click.echo()
    echo_success("Retry complete!")
    echo_info("Run 'aap-bridge retry status' to see updated progress")


@retry_group.command(name="status")
@click.option(
    "--resource-type",
    "-r",
    multiple=True,
    help="Show status for specific resource types only",
)
@pass_context
@requires_config
@handle_errors
def retry_status(ctx: MigrationContext, resource_type: tuple) -> None:
    """Show retry/resume status.

    Displays which resources are pending, failed, or completed.

    Examples:

        # Show overall status
        aap-bridge retry status

        # Show status for specific types
        aap-bridge retry status -r credentials -r projects
    """
    from sqlalchemy import func
    from aap_migration.migration.database import get_session
    from aap_migration.migration.models import MigrationProgress

    console = Console()
    state = ctx.migration_state

    # Get status summary using proper session
    with get_session(state.database_url) as session:
        query = session.query(
            MigrationProgress.resource_type,
            MigrationProgress.status,
            func.count(MigrationProgress.id).label('count')
        )

        if resource_type:
            query = query.filter(MigrationProgress.resource_type.in_(resource_type))

        query = query.group_by(
            MigrationProgress.resource_type,
            MigrationProgress.status
        ).order_by(
            MigrationProgress.resource_type,
            MigrationProgress.status
        )

        rows = query.all()

    # Organize by resource type
    by_type = {}
    for row in rows:
        rtype = row[0]
        status = row[1] or "pending"
        count = row[2]

        if rtype not in by_type:
            by_type[rtype] = {"completed": 0, "failed": 0, "pending": 0, "in_progress": 0}

        by_type[rtype][status] = count

    if not by_type:
        echo_info("No import progress found")
        return

    # Create status table
    table = Table(title="Import/Retry Status", show_header=True, header_style="bold cyan")
    table.add_column("Resource Type", style="cyan", width=25)
    table.add_column("Completed", justify="right", width=10, style="green")
    table.add_column("Failed", justify="right", width=10, style="red")
    table.add_column("Pending", justify="right", width=10, style="yellow")
    table.add_column("In Progress", justify="right", width=12, style="blue")
    table.add_column("Total", justify="right", width=10)

    for rtype, counts in by_type.items():
        total = sum(counts.values())
        table.add_row(
            rtype,
            str(counts["completed"]) if counts["completed"] > 0 else "-",
            str(counts["failed"]) if counts["failed"] > 0 else "-",
            str(counts["pending"]) if counts["pending"] > 0 else "-",
            str(counts["in_progress"]) if counts["in_progress"] > 0 else "-",
            str(total),
        )

    console.print("\n")
    console.print(table)
    console.print("\n")
