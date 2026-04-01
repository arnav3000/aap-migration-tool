"""Granular micro-phase import with step-by-step control."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from aap_migration.cli.utils import echo_error, echo_info, echo_success, echo_warning
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


# Define granular micro-phases in dependency order
# Follows EXACT proven migration order from manual testing
MICRO_PHASES = [
    # Phase 1: Foundation
    {"id": "1.1", "name": "Organizations", "resource_type": "organizations"},
    {"id": "1.2", "name": "Users", "resource_type": "users"},
    {"id": "1.3", "name": "Teams", "resource_type": "teams"},

    # Phase 2: Credentials (CRITICAL - must be 100% complete)
    {"id": "2.1", "name": "Credential Types", "resource_type": "credential_types"},
    {"id": "2.2", "name": "Credentials", "resource_type": "credentials"},

    # Phase 3: Infrastructure (MUST follow this order)
    {"id": "3.1", "name": "Execution Environments", "resource_type": "execution_environments"},
    {"id": "3.2", "name": "Projects", "resource_type": "projects"},
    {"id": "3.3", "name": "Inventories", "resource_type": "inventories"},
    {"id": "3.4", "name": "Inventory Sources", "resource_type": "inventory_sources"},

    # Phase 4: Hosts
    {"id": "4.1", "name": "Hosts", "resource_type": "hosts"},

    # Phase 5: Instance Groups
    {"id": "5.1", "name": "Instance Groups", "resource_type": "instance_groups"},

    # Phase 6: Automation
    {"id": "6.1", "name": "Job Templates", "resource_type": "job_templates"},
    {"id": "6.2", "name": "Workflow Templates", "resource_type": "workflow_job_templates"},

    # Phase 7: Schedules
    {"id": "7.1", "name": "Schedules", "resource_type": "schedules"},

    # Phase 8: Applications (OAuth applications)
    {"id": "8.1", "name": "Applications", "resource_type": "applications"},

    # Phase 9: Settings (Optional - review before applying)
    {"id": "9.1", "name": "Settings", "resource_type": "settings", "optional": True},

    # Phase 10: RBAC (after main migration)
    {"id": "10.1", "name": "RBAC Role Assignments", "resource_type": "rbac", "special": "rbac_script"},
]


class GranularImporter:
    """Granular step-by-step importer with micro-phase control."""

    def __init__(self, ctx: Any, input_dir: Path):
        """Initialize granular importer.

        Args:
            ctx: Migration context
            input_dir: Directory with transformed data
        """
        self.ctx = ctx
        self.input_dir = input_dir
        self.console = Console()
        self.state = ctx.migration_state

        # Load metadata
        metadata_file = input_dir / "metadata.json"
        with open(metadata_file) as f:
            self.metadata = json.load(f)

    def get_resource_count(self, resource_type: str) -> int:
        """Get count of resources for a type.

        Args:
            resource_type: Resource type name

        Returns:
            Number of resources
        """
        return self.metadata.get("resource_types", {}).get(resource_type, {}).get("count", 0)

    def get_import_stats(self, resource_type: str) -> dict[str, int]:
        """Get import statistics for a resource type.

        Args:
            resource_type: Resource type name

        Returns:
            Dictionary with completed, failed, pending counts
        """
        stats = self.state.get_import_stats(resource_type)
        return {
            "total": stats.get("total_exported", 0),
            "completed": stats.get("completed", 0),
            "failed": stats.get("failed", 0),
            "pending": stats.get("pending", 0),
        }

    def create_phase_table(self, current_phase_id: str | None = None) -> Table:
        """Create table showing all micro-phases.

        Args:
            current_phase_id: ID of currently executing phase

        Returns:
            Rich Table
        """
        table = Table(show_header=True, header_style="bold cyan", box=None)
        table.add_column("Phase", style="cyan", width=8)
        table.add_column("Resource Type", width=25)
        table.add_column("Total", justify="right", width=8)
        table.add_column("✓", justify="right", width=6, style="green")
        table.add_column("✗", justify="right", width=6, style="red")
        table.add_column("⧗", justify="right", width=6, style="yellow")
        table.add_column("Progress", width=25)
        table.add_column("Status", width=12)

        for micro_phase in MICRO_PHASES:
            phase_id = micro_phase["id"]
            resource_type = micro_phase["resource_type"]
            name = micro_phase["name"]

            total = self.get_resource_count(resource_type)

            if total == 0:
                # Skip phases with no resources
                continue

            stats = self.get_import_stats(resource_type)

            # Progress bar
            if stats["total"] > 0:
                percent = (stats["completed"] / stats["total"]) * 100
                bar_length = 15
                filled = int(bar_length * percent / 100)
                bar = "█" * filled + "░" * (bar_length - filled)
                progress = f"{bar} {percent:.0f}%"
            else:
                progress = "░" * 15 + " 0%"

            # Status
            if phase_id == current_phase_id:
                status = "[bold yellow]→ Running[/bold yellow]"
                style = "bold yellow"
            elif stats["completed"] == stats["total"]:
                status = "[green]✓ Done[/green]"
                style = "dim"
            elif stats["failed"] > 0:
                status = "[red]✗ Errors[/red]"
                style = "red"
            elif stats["completed"] > 0:
                status = "[yellow]⧗ Partial[/yellow]"
                style = ""
            else:
                status = "[dim]⧗ Pending[/dim]"
                style = "dim"

            table.add_row(
                phase_id,
                name,
                str(stats["total"]) if stats["total"] > 0 else "-",
                str(stats["completed"]) if stats["completed"] > 0 else "-",
                str(stats["failed"]) if stats["failed"] > 0 else "-",
                str(stats["pending"]) if stats["pending"] > 0 else "-",
                progress,
                status,
                style=style,
            )

        return table

    def _import_micro_phase(self, micro_phase: dict) -> dict[str, Any]:
        """Import a single micro-phase using the proven migrate command.

        Args:
            micro_phase: Micro-phase definition

        Returns:
            Result dictionary with counts
        """
        resource_type = micro_phase["resource_type"]
        phase_id = micro_phase["id"]
        name = micro_phase["name"]
        special_handler = micro_phase.get("special")

        self.console.print(f"\n[bold cyan]Phase {phase_id}: {name}[/bold cyan]\n")

        # Build config path argument (only if config_path exists)
        config_arg = []
        if hasattr(self.ctx, 'config_path') and self.ctx.config_path:
            config_arg = ["--config", str(self.ctx.config_path)]

        # Handle RBAC special case (uses rbac_migration.py script)
        if special_handler == "rbac_script":
            echo_info("Running RBAC migration script...")
            echo_warning("Note: RBAC migration uses rbac_migration.py (not migrate command)")

            # Check if rbac_migration.py exists
            rbac_script = Path("rbac_migration.py")
            if not rbac_script.exists():
                echo_error("rbac_migration.py not found in current directory")
                return {"total": 0, "completed": 0, "failed": 0, "skipped": 0}

            cmd = [sys.executable, str(rbac_script)]

            try:
                result = subprocess.run(
                    cmd,
                    check=False,
                    capture_output=False,  # Show output in real-time
                    text=True
                )

                if result.returncode == 0:
                    echo_success("RBAC migration completed")
                    return {"total": 1, "completed": 1, "failed": 0, "skipped": 0}
                else:
                    echo_warning("RBAC migration finished with errors")
                    return {"total": 1, "completed": 0, "failed": 1, "skipped": 0}

            except Exception as e:
                echo_error(f"Failed to run RBAC migration: {e}")
                return {"total": 0, "completed": 0, "failed": 0, "skipped": 0}

        # For regular resources, check if we have resources to import
        # Settings doesn't need transformed data (exports directly)
        if resource_type != "settings":
            resource_dir = self.input_dir / resource_type
            if not resource_dir.exists():
                echo_warning(f"No data found for {resource_type}")
                return {"total": 0, "completed": 0, "failed": 0, "skipped": 0}

        # Get stats BEFORE import
        stats_before = self.get_import_stats(resource_type)

        # Build command using the proven migrate command
        cmd = [
            sys.executable, "-m", "aap_migration.cli.main",
        ] + config_arg + [
            "migrate",
            "-r", resource_type,
            "--skip-prep",
            "--phase", "all"
        ]

        echo_info(f"Importing {name}...")

        try:
            # Run the proven migrate command
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=False,  # Show output in real-time
                text=True
            )

            # Get stats AFTER import
            stats_after = self.get_import_stats(resource_type)

            completed = stats_after["completed"] - stats_before["completed"]
            failed = stats_after["failed"] - stats_before["failed"]

            return {
                "total": stats_after["total"],
                "completed": completed,
                "failed": failed,
                "skipped": 0,
            }

        except Exception as e:
            echo_error(f"Failed to import {name}: {e}")
            return {
                "total": stats_before["total"],
                "completed": 0,
                "failed": 0,
                "skipped": 0,
            }

    def run(self) -> None:
        """Run granular import with step-by-step control."""
        self.console.clear()
        self.console.print(
            Panel.fit(
                "[bold cyan]Granular Micro-Phase Import[/bold cyan]\n\n"
                "Import resources step-by-step with full control.\n"
                "You can skip phases, retry, or abort at any time.",
                border_style="cyan",
            )
        )

        # Show initial status
        self.console.print("\n")
        self.console.print(self.create_phase_table())
        self.console.print("\n")

        Prompt.ask("Press Enter to start...")

        # Execute each micro-phase
        for micro_phase in MICRO_PHASES:
            phase_id = micro_phase["id"]
            resource_type = micro_phase["resource_type"]
            name = micro_phase["name"]

            # Skip if no resources
            total = self.get_resource_count(resource_type)
            if total == 0:
                continue

            # Show current status
            self.console.clear()
            self.console.print(f"\n[bold]Import Progress[/bold]\n")
            self.console.print(self.create_phase_table(current_phase_id=phase_id))
            self.console.print("\n")

            # Check if already completed
            stats = self.get_import_stats(resource_type)
            if stats["completed"] == stats["total"] and stats["failed"] == 0:
                echo_success(f"Phase {phase_id}: {name} - Already completed, skipping")
                continue

            # Ask user what to do
            self.console.print(f"[bold cyan]Phase {phase_id}: {name}[/bold cyan]")
            self.console.print(f"  Total: {stats['total']}")
            self.console.print(f"  Completed: {stats['completed']}")
            if stats["failed"] > 0:
                self.console.print(f"  [red]Failed: {stats['failed']}[/red]")
            if stats["pending"] > 0:
                self.console.print(f"  Pending: {stats['pending']}")
            self.console.print()

            self.console.print("[bold]Actions:[/bold]")
            self.console.print("  [cyan]i[/cyan] - Import this phase")
            self.console.print("  [yellow]s[/yellow] - Skip this phase (continue to next)")
            self.console.print("  [blue]r[/blue] - Retry failed resources in this phase")
            self.console.print("  [magenta]v[/magenta] - View errors for this phase")
            self.console.print("  [red]a[/red] - Abort entire import")
            self.console.print()

            choice = Prompt.ask(
                "Select action",
                choices=["i", "s", "r", "v", "a"],
                default="i",
            )

            if choice == "s":
                echo_warning(f"Skipping phase {phase_id}")
                continue

            elif choice == "a":
                echo_warning("Import aborted by user")
                break

            elif choice == "v":
                # View errors
                from aap_migration.migration.db import get_session, MigrationProgress

                with get_session(self.state.database_url) as session:
                    error_records = (
                        session.query(
                            MigrationProgress.source_id,
                            MigrationProgress.source_name,
                            MigrationProgress.error_message
                        )
                        .filter(
                            MigrationProgress.resource_type == resource_type,
                            MigrationProgress.status == 'failed'
                        )
                        .limit(10)
                        .all()
                    )

                if error_records:
                    self.console.print("\n[bold red]Failed Resources:[/bold red]")
                    for err in error_records:
                        self.console.print(f"  • {err[1]} (ID:{err[0]}): {err[2]}")
                    self.console.print()
                else:
                    self.console.print("\n[green]No errors[/green]\n")

                Prompt.ask("Press Enter to continue...")
                # Re-show menu for this phase
                continue

            elif choice == "r":
                # Retry failed resources
                echo_info("Retrying failed resources...")

            # Import this phase
            result = self._import_micro_phase(micro_phase)

            # Show result
            self.console.print()
            if result["failed"] > 0:
                echo_warning(
                    f"Phase {phase_id} completed with errors: "
                    f"{result['completed']} succeeded, {result['failed']} failed"
                )

                # Ask if continue
                choice = Prompt.ask(
                    "Continue to next phase?",
                    choices=["y", "n", "r"],
                    default="y",
                )

                if choice == "n":
                    echo_warning("Import stopped by user")
                    break
                elif choice == "r":
                    # TODO: Implement retry for this phase
                    echo_warning("Retry not yet implemented, continuing...")
            else:
                echo_success(
                    f"Phase {phase_id} completed: {result['completed']} resources imported"
                )

            Prompt.ask("\nPress Enter to continue...")

        # Final summary
        self.console.clear()
        self.console.print("\n[bold green]Import Complete![/bold green]\n")
        self.console.print(self.create_phase_table())
        self.console.print("\n")


def granular_import_menu(ctx: Any, input_dir: Path | None = None) -> None:
    """Launch granular import menu.

    Args:
        ctx: Migration context
        input_dir: Input directory with transformed data
    """
    from pathlib import Path

    if input_dir is None:
        input_dir = Path(ctx.obj.config.paths.transform_dir)

    importer = GranularImporter(ctx.obj, input_dir)
    importer.run()
