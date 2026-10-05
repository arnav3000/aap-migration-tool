"""Retry and resume commands for failed imports."""

import subprocess
import sys
from pathlib import Path
from typing import Any

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


#: Default timeout for each per-type ``migrate`` child process. Owned by
#: the CLI so retry works on CLI-only installs; a future API layer may pass
#: its own job timeout through instead of importing this private constant.
RETRY_CHILD_TIMEOUT_SECS = 3600.0


def _retry_child_timeout_secs() -> float:
    """Timeout for each per-type ``migrate`` child process.

    Owned by the CLI (:data:`RETRY_CHILD_TIMEOUT_SECS`, overridable via
    ``AAP_BRIDGE_JOB_TIMEOUT_SECS`` for API workers) so a hung child cannot
    wedge the single FIFO worker forever. A hung child is killed via
    ``subprocess`` timeout and its rows are re-marked failed so the next
    retry sees the truth.
    """
    import os

    try:
        return max(
            float(os.environ.get("AAP_BRIDGE_JOB_TIMEOUT_SECS", RETRY_CHILD_TIMEOUT_SECS)), 1.0
        )
    except (TypeError, ValueError):
        return float(RETRY_CHILD_TIMEOUT_SECS)


@click.group(name="retry", hidden=True)
def retry_group() -> None:
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
            MigrationProgress.error_message,
        ).filter(MigrationProgress.status == "failed")

        if resource_type:
            query = query.filter(MigrationProgress.resource_type.in_(resource_type))

        query = query.order_by(MigrationProgress.resource_type, MigrationProgress.source_id)

        failed_resources = query.all()

    if not failed_resources:
        echo_success("No failed resources to retry!")
        return

    # Group by resource type
    grouped = _group_failed_resources(failed_resources)

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

    # Flip failed rows to pending per type just before each child runs (not
    # upfront): a crash then strands at most the in-flight type's rows, and
    # the startup sweep below recovers rows stranded by an earlier run.
    click.echo()
    echo_info("Clearing failed status to enable retry...")

    # Build config path argument
    config_arg = []
    if ctx.config_path:
        config_arg = ["--config", str(ctx.config_path)]

    # Each child runs with an explicit timeout: on expiry the whole process
    # group is killed (so detached grandchildren cannot keep writing the
    # shared state DB while the next type starts) and the type is recorded
    # as a timeout error instead of wedging the single FIFO worker.
    child_timeout = _retry_child_timeout_secs()

    # Recover rows stranded by an earlier interrupted retry (SIGKILL/power
    # loss between the pending flip and the child finishing): stale
    # pending/in_progress rows predate this run, so they cannot be work this
    # command flipped moments ago. Re-query afterwards so recovered rows join
    # this run.
    recovered = _sweep_stranded_to_failed(state, list(grouped.keys()), child_timeout)
    if recovered:
        echo_info(f"Recovered {recovered} stranded rows from an earlier interrupted retry")
        with get_session(state.database_url) as session:
            query = session.query(
                MigrationProgress.resource_type,
                MigrationProgress.source_id,
                MigrationProgress.source_name,
                MigrationProgress.error_message,
            ).filter(MigrationProgress.status == "failed")

            if resource_type:
                query = query.filter(MigrationProgress.resource_type.in_(resource_type))

            query = query.order_by(MigrationProgress.resource_type, MigrationProgress.source_id)

            failed_resources = query.all()

        grouped = _group_failed_resources(failed_resources)

    echo_success(f"Found {len(failed_resources)} failed resources to retry")

    # Now run import using the proven migrate command
    click.echo()
    echo_info("Starting import of previously failed resources...")
    echo_info("(Using proven migrate command to retry)")
    click.echo()

    # Import each resource type that had failures using proven migrate command.
    failed_types: list[str] = []
    current: str | None = None
    try:
        for rtype in grouped.keys():
            current = rtype
            _flip_type_to_pending(state, rtype)
            echo_info(f"Retrying {rtype}... (timeout {child_timeout:g}s)")

            # Build command using the proven migrate command
            cmd = (
                [
                    sys.executable,
                    "-m",
                    "aap_migration.cli.main",
                ]
                + config_arg
                + ["migrate", "-r", rtype, "--skip-prep", "--phase", "all"]
            )

            try:
                # Run the proven migrate command in its own process group so a
                # timeout can reap the whole tree (POSIX killpg; fallback to
                # child kill on platforms without process groups).
                returncode = _run_child_in_group(cmd, timeout=child_timeout)

                if returncode == 0:
                    echo_success(f"  ✓ {rtype} retry completed")
                else:
                    echo_warning(f"  ⚠ {rtype} retry finished with errors")
                    failed_types.append(rtype)
                    if not _mark_type_failed(state, rtype):
                        echo_warning(
                            f"  ⚠ {rtype}: pending rows could not be re-marked as "
                            "failed; retry status may miss them"
                        )

            except subprocess.TimeoutExpired:
                echo_error(
                    f"Retry of {rtype} timed out after {child_timeout:g}s; "
                    "process group killed, continuing with next type"
                )
                failed_types.append(rtype)
                if not _mark_type_failed(state, rtype, note="timeout"):
                    echo_warning(
                        f"  ⚠ {rtype}: pending rows could not be re-marked as "
                        "failed; retry status may miss them"
                    )
                continue
            except Exception as e:
                echo_error(f"Failed to retry {rtype}: {e}")
                failed_types.append(rtype)
                if not _mark_type_failed(state, rtype, note=str(e)):
                    echo_warning(
                        f"  ⚠ {rtype}: pending rows could not be re-marked as "
                        "failed; retry status may miss them"
                    )
                continue
    except BaseException:
        # KeyboardInterrupt/SystemExit (SIGKILL and power loss cannot be
        # caught): re-mark the in-flight type so the next retry sees failed,
        # not pending, then re-raise.
        if current is not None:
            _mark_type_failed(state, current, note="interrupted")
        raise

    click.echo()
    if failed_types:
        from click import ClickException

        raise ClickException(
            f"Retry failed for: {', '.join(failed_types)}. "
            "Their rows were re-marked 'failed'; fix the cause and retry again."
        )
    echo_success("Retry complete!")
    echo_info("Run 'aap-bridge retry status' to see updated progress")


def _run_child_in_group(cmd: list[str], timeout: float) -> int:
    """Run *cmd* in its own process group, killing the group on timeout.

    Returns the child return code. Raises :class:`subprocess.TimeoutExpired`
    after the group has been reaped so callers can re-mark rows failed.
    Detached grandchildren can otherwise outlive a plain
    :func:`subprocess.run` timeout kill and keep writing the shared state
    DB while the next resource type starts (status flap + SQLite-busy
    cascading failures).
    """
    import os
    import signal

    proc = subprocess.Popen(cmd, start_new_session=True, text=True)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        finally:
            try:
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        raise


def _group_failed_resources(failed_resources: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """Group failed-resource rows by resource type (shared by initial and post-sweep queries)."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in failed_resources:
        rtype = row[0]
        if rtype not in grouped:
            grouped[rtype] = []
        grouped[rtype].append(
            {
                "source_id": row[1],
                "name": row[2],
                "error": row[3],
            }
        )
    return grouped


def _flip_type_to_pending(state: Any, rtype: str) -> int:
    """Flip failed rows of *rtype* to pending just before its child runs.

    Scoped per type so a crash strands at most the in-flight type's rows
    (recovered by the startup sweep), never the whole retry set. Returns
    the flipped count.
    """
    from aap_migration.migration.database import get_session
    from aap_migration.migration.models import MigrationProgress

    with get_session(state.database_url) as session:
        rows = (
            session.query(MigrationProgress).filter_by(resource_type=rtype, status="failed").all()
        )
        for row in rows:
            # Status column is NOT NULL; None would raise IntegrityError.
            row.status = "pending"
        session.commit()
        return len(rows)


def _sweep_stranded_to_failed(state: Any, rtypes: list[str], stale_after_secs: float) -> int:
    """Re-mark stale pending/in_progress rows of *rtypes* as failed.

    Closes the crash window no in-process handler can cover: SIGKILL or
    power loss between the per-type pending flip and the child finishing
    leaves rows that a future ``retry failed`` (which selects only
    ``failed``) would never see. Only rows whose ``updated_at`` predates
    the staleness cutoff are touched, so work flipped moments ago by this
    run is never swept. Assumes one job owns this state DB; concurrent
    same-DB jobs would need a worker-id guard. Returns the recovered count.
    """
    from datetime import UTC, datetime, timedelta

    from aap_migration.migration.database import get_session
    from aap_migration.migration.models import MigrationProgress

    if not rtypes:
        return 0
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=max(stale_after_secs, 1.0))
    with get_session(state.database_url) as session:
        rows = (
            session.query(MigrationProgress)
            .filter(
                MigrationProgress.resource_type.in_(rtypes),
                MigrationProgress.status.in_(["pending", "in_progress"]),
                MigrationProgress.updated_at < cutoff,
            )
            .all()
        )
        for row in rows:
            previous = row.status
            row.status = "failed"
            note = f"recovered stranded {previous} (retry interrupted)"
            row.error_message = f"{row.error_message}\n{note}"[:2000] if row.error_message else note
        session.commit()
        return len(rows)


def _mark_type_failed(state: Any, rtype: str, note: str | None = None) -> bool:
    """Re-mark still-unfinished rows of *rtype* as failed (best-effort).

    Flips both ``pending`` (flipped by this retry before the child ran)
    and ``in_progress`` (marked by importers before target writes; a
    timeout SIGKILLs the child mid-row with no cleanup) back to ``failed``
    so the next status/retry sees the truth. ``error_message`` is only
    touched when *note* is given (``None`` preserves the original failure
    reason). Returns True on success, False when the re-mark itself
    failed: callers must not silently strand rows where the next
    retry-failed (which selects only ``failed`` rows) reports "No failed
    resources to retry" while work sits unattempted.
    """
    try:
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import MigrationProgress

        with get_session(state.database_url) as session:
            rows = (
                session.query(MigrationProgress)
                .filter(
                    MigrationProgress.resource_type == rtype,
                    MigrationProgress.status.in_(["pending", "in_progress"]),
                )
                .all()
            )
            for row in rows:
                row.status = "failed"
                if note:
                    row.error_message = f"retry {note}"[:2000]
            session.commit()
        return True
    except Exception as exc:
        logger.warning("re-mark pending/in_progress->failed failed for %s: %r", rtype, exc)
        return False


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
            func.count(MigrationProgress.id).label("count"),
        )

        if resource_type:
            query = query.filter(MigrationProgress.resource_type.in_(resource_type))

        query = query.group_by(MigrationProgress.resource_type, MigrationProgress.status).order_by(
            MigrationProgress.resource_type, MigrationProgress.status
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
