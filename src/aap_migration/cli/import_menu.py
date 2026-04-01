"""Enhanced import menu with dependency validation and error handling."""

import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from aap_migration.cli.granular_import import granular_import_menu
from aap_migration.cli.utils import echo_error, echo_info, echo_success, echo_warning


def run_command(args: list[str], ctx: Any = None) -> int:
    """Run a CLI command and return exit code."""
    import subprocess

    cmd = [sys.argv[0]]

    if ctx and ctx.obj and ctx.obj.config_path:
        cmd.extend(["--config", str(ctx.obj.config_path)])

    cmd.extend(args)

    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except Exception as e:
        echo_error(f"Error running command: {e}")
        return 1


def show_import_status(ctx: Any) -> None:
    """Display current import status and progress."""
    console = Console()

    try:
        # Use the migration state from context
        state = ctx.obj.migration_state

        # Get all resource types with progress
        all_stats = []
        resource_types = [
            "organizations", "users", "teams", "credential_types",
            "credentials", "execution_environments", "projects",
            "inventories", "inventory_sources", "hosts",
            "instance_groups", "job_templates",
            "workflow_job_templates", "schedules", "applications",
            "settings"
        ]

        # Need to query MigrationProgress for completed/failed counts
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import MigrationProgress
        from sqlalchemy import func

        with get_session(state.database_url) as session:
            for rtype in resource_types:
                stats = state.get_import_stats(rtype)
                if stats["total_exported"] > 0:
                    # Get completed and failed counts from MigrationProgress
                    completed = (
                        session.query(func.count(MigrationProgress.id))
                        .filter(
                            MigrationProgress.resource_type == rtype,
                            MigrationProgress.status == 'completed'
                        )
                        .scalar() or 0
                    )

                    failed = (
                        session.query(func.count(MigrationProgress.id))
                        .filter(
                            MigrationProgress.resource_type == rtype,
                            MigrationProgress.status == 'failed'
                        )
                        .scalar() or 0
                    )

                    all_stats.append({
                        "type": rtype,
                        "total": stats["total_exported"],
                        "completed": completed,
                        "failed": failed,
                        "pending": stats["pending"],
                        "percent": stats["percent_complete"]
                    })

        if not all_stats:
            console.print("\n[yellow]No import progress found. Run export first.[/yellow]\n")
            return

        # Create status table
        table = Table(title="Import Status", show_header=True, header_style="bold cyan")
        table.add_column("Resource Type", style="cyan", width=25)
        table.add_column("Total", justify="right", width=8)
        table.add_column("Completed", justify="right", width=10, style="green")
        table.add_column("Failed", justify="right", width=8, style="red")
        table.add_column("Pending", justify="right", width=8, style="yellow")
        table.add_column("Progress", width=20)

        for stat in all_stats:
            # Progress bar
            percent = stat["percent"]
            bar_length = 15
            filled = int(bar_length * percent / 100)
            bar = "█" * filled + "░" * (bar_length - filled)

            # Color code based on status
            if stat["failed"] > 0:
                status_color = "red"
            elif stat["pending"] > 0:
                status_color = "yellow"
            else:
                status_color = "green"

            table.add_row(
                stat["type"],
                str(stat["total"]),
                str(stat["completed"]),
                str(stat["failed"]) if stat["failed"] > 0 else "-",
                str(stat["pending"]) if stat["pending"] > 0 else "-",
                f"[{status_color}]{bar}[/{status_color}] {percent:.1f}%"
            )

        console.print("\n")
        console.print(table)
        console.print("\n")

        # Summary
        totals = {
            "total": sum(s["total"] for s in all_stats),
            "completed": sum(s["completed"] for s in all_stats),
            "failed": sum(s["failed"] for s in all_stats),
            "pending": sum(s["pending"] for s in all_stats),
        }

        console.print(f"[bold]Overall Progress:[/bold]")
        console.print(f"  Total Resources: {totals['total']}")
        console.print(f"  [green]✓ Completed: {totals['completed']}[/green]")
        if totals['failed'] > 0:
            console.print(f"  [red]✗ Failed: {totals['failed']}[/red]")
        if totals['pending'] > 0:
            console.print(f"  [yellow]⧗ Pending: {totals['pending']}[/yellow]")
        console.print()

    except Exception as e:
        import traceback
        echo_error(f"Failed to get import status: {e}")
        console.print(f"\n[red]Error details:[/red]")
        console.print(traceback.format_exc())


def show_failed_resources(ctx: Any) -> None:
    """Display failed resources with error details."""
    console = Console()

    try:
        # Use the migration state from context
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import MigrationProgress

        state = ctx.obj.migration_state

        # Query failed resources using proper session
        with get_session(state.database_url) as session:
            failed_records = (
                session.query(
                    MigrationProgress.resource_type,
                    MigrationProgress.source_id,
                    MigrationProgress.source_name,
                    MigrationProgress.error_message,
                    MigrationProgress.updated_at
                )
                .filter(MigrationProgress.status == 'failed')
                .order_by(MigrationProgress.resource_type, MigrationProgress.source_id)
                .all()
            )

        if not failed_records:
            console.print("\n[green]✓ No failed resources![/green]\n")
            return

        # Create failed resources table
        table = Table(title="Failed Resources", show_header=True, header_style="bold red")
        table.add_column("Type", style="cyan", width=20)
        table.add_column("Source ID", justify="right", width=10)
        table.add_column("Name", width=25)
        table.add_column("Error", width=50)

        for row in failed_records:
            error = row[3] if row[3] else "Unknown error"
            # Truncate long errors
            if len(error) > 47:
                error = error[:44] + "..."

            table.add_row(
                row[0],  # resource_type
                str(row[1]),  # source_id
                row[2] if row[2] else "N/A",  # source_name
                error
            )

        console.print("\n")
        console.print(table)
        console.print(f"\n[bold red]Total Failed: {len(failed_records)}[/bold red]\n")

    except Exception as e:
        import traceback
        echo_error(f"Failed to get error details: {e}")
        console.print(f"\n[red]Error details:[/red]")
        console.print(traceback.format_exc())


def import_submenu(ctx: Any) -> None:
    """Enhanced import submenu with validation and retry options."""
    console = Console()

    while True:
        console.clear()
        console.print(
            Panel.fit(
                "[bold cyan]Import Resources[/bold cyan]\n\n"
                "1. Pre-flight Check (Validate Dependencies)\n"
                "2. Import All Resources (Automatic)\n"
                "3. Granular Import (Step-by-Step Control) ⭐ Recommended\n"
                "4. Retry Failed Resources\n"
                "5. View Import Status\n"
                "6. View Failed Resources\n"
                "b. Back to Main Menu",
                title="Import Menu",
                border_style="cyan",
            )
        )

        choice = Prompt.ask(
            "Select an option",
            choices=["1", "2", "3", "4", "5", "6", "b"],
            default="b"
        )

        if choice.lower() == "b":
            break

        console.print()

        if choice == "1":
            # Pre-flight dependency check
            echo_info("Running pre-flight dependency validation...")
            run_command(["import", "--check-dependencies"])

        elif choice == "2":
            # Import all resources
            console.print("[yellow]This will import all resources in the correct order.[/yellow]")
            confirm = Prompt.ask("Continue?", choices=["y", "n"], default="n")
            if confirm == "y":
                run_command(["import"])

        elif choice == "3":
            # Granular step-by-step import
            console.print("[yellow]Import resources one phase at a time with full control.[/yellow]")
            console.print("[dim]You can skip, retry, or abort at any phase.[/dim]")
            confirm = Prompt.ask("Continue?", choices=["y", "n"], default="n")
            if confirm == "y":
                granular_import_menu(ctx)

        elif choice == "4":
            # Retry failed
            console.print("[yellow]This will retry all previously failed resources.[/yellow]")
            confirm = Prompt.ask("Continue?", choices=["y", "n"], default="n")
            if confirm == "y":
                run_command(["retry", "failed", "-y"])

        elif choice == "5":
            # View status
            show_import_status(ctx)

        elif choice == "6":
            # View failed resources
            show_failed_resources(ctx)

        if choice not in ["5", "6"]:
            Prompt.ask("\nPress Enter to continue...")
