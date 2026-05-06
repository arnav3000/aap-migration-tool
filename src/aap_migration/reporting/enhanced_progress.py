"""Enhanced progress display with granular resource tracking and error details."""

from typing import Any

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn
from rich.table import Table

from aap_migration.migration.state import MigrationState
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class EnhancedProgressDisplay:
    """Enhanced progress display for migration with error tracking."""

    def __init__(self, state: MigrationState):
        """Initialize enhanced progress display.

        Args:
            state: Migration state manager
        """
        self.state = state
        self.console = Console()

        # Progress bars for each resource type
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}", justify="left"),
            BarColumn(bar_width=30),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("•"),
            TextColumn("[green]{task.completed}/{task.total}"),
            TextColumn("•"),
            TextColumn("[red]❌{task.fields[failed]}"),
            TimeElapsedColumn(),
        )

        # Track tasks by resource type
        self.tasks: dict[str, TaskID] = {}

        # Track errors
        self.errors: list[dict[str, Any]] = []

        # Overall summary
        self.totals = {
            "total": 0,
            "completed": 0,
            "failed": 0,
            "pending": 0,
        }

    def add_resource_type(self, resource_type: str, total: int) -> None:
        """Add a resource type to track.

        Args:
            resource_type: Resource type name
            total: Total number of resources
        """
        task_id = self.progress.add_task(
            f"{resource_type:25s}",
            total=total,
            failed=0,
        )
        self.tasks[resource_type] = task_id
        self.totals["total"] += total
        self.totals["pending"] += total

    def update_progress(
        self,
        resource_type: str,
        completed: int | None = None,
        failed: int | None = None,
        advance: int = 0,
    ) -> None:
        """Update progress for a resource type.

        Args:
            resource_type: Resource type name
            completed: New completed count (or None to not update)
            failed: New failed count (or None to not update)
            advance: Number to advance by (default: 0)
        """
        if resource_type not in self.tasks:
            return

        task_id = self.tasks[resource_type]

        if advance > 0:
            self.progress.update(task_id, advance=advance)
            self.totals["completed"] += advance
            self.totals["pending"] -= advance

        if failed is not None:
            current_failed = self.progress.tasks[task_id].fields.get("failed", 0)
            delta = failed - current_failed
            self.progress.update(task_id, failed=failed)
            self.totals["failed"] += delta
            if delta > 0:
                self.totals["pending"] -= delta

    def add_error(
        self,
        resource_type: str,
        source_id: int,
        name: str,
        error: str,
    ) -> None:
        """Record an error.

        Args:
            resource_type: Resource type
            source_id: Source resource ID
            name: Resource name
            error: Error message
        """
        self.errors.append({
            "type": resource_type,
            "source_id": source_id,
            "name": name,
            "error": error,
        })

        # Update failed count
        task_id = self.tasks.get(resource_type)
        if task_id:
            current_failed = self.progress.tasks[task_id].fields.get("failed", 0)
            self.update_progress(resource_type, failed=current_failed + 1)

    def create_summary_panel(self) -> Panel:
        """Create summary panel with overall stats.

        Returns:
            Rich Panel with summary
        """
        summary_table = Table.grid(padding=(0, 2))
        summary_table.add_column(style="bold", justify="right")
        summary_table.add_column()

        summary_table.add_row("Total Resources:", f"[cyan]{self.totals['total']}[/cyan]")
        summary_table.add_row("Completed:", f"[green]{self.totals['completed']}[/green]")
        summary_table.add_row("Failed:", f"[red]{self.totals['failed']}[/red]")
        summary_table.add_row("Pending:", f"[yellow]{self.totals['pending']}[/yellow]")

        if self.totals["total"] > 0:
            percent = (self.totals["completed"] / self.totals["total"]) * 100
            summary_table.add_row("Progress:", f"[bold]{percent:.1f}%[/bold]")

        return Panel(summary_table, title="[bold cyan]Overall Progress[/bold cyan]", border_style="cyan")

    def create_errors_panel(self) -> Panel | None:
        """Create panel showing recent errors.

        Returns:
            Rich Panel with errors, or None if no errors
        """
        if not self.errors:
            return None

        error_table = Table(show_header=True, header_style="bold red", box=None)
        error_table.add_column("Type", style="cyan", width=20)
        error_table.add_column("Resource", width=25)
        error_table.add_column("Error", width=50)

        # Show last 5 errors
        for error in self.errors[-5:]:
            error_msg = error["error"]
            if len(error_msg) > 47:
                error_msg = error_msg[:44] + "..."

            error_table.add_row(
                error["type"],
                f"{error['name']} (ID:{error['source_id']})",
                error_msg,
            )

        title = f"[bold red]Recent Errors ({len(self.errors)} total)[/bold red]"
        return Panel(error_table, title=title, border_style="red")

    def get_display_layout(self) -> Table:
        """Get the full display layout.

        Returns:
            Rich Table with complete layout
        """
        layout = Table.grid(padding=(1, 0))
        layout.add_column()

        # Summary panel
        layout.add_row(self.create_summary_panel())

        # Progress bars
        layout.add_row(Panel(self.progress, title="[bold]Resource Import Progress[/bold]", border_style="blue"))

        # Errors panel (if any)
        errors_panel = self.create_errors_panel()
        if errors_panel:
            layout.add_row(errors_panel)

        return layout

    def run_with_live_display(self, import_func):
        """Run import function with live progress display.

        Args:
            import_func: Function that performs the import

        Returns:
            Result from import_func
        """
        with Live(self.get_display_layout(), console=self.console, refresh_per_second=4) as live:
            self.live = live

            try:
                result = import_func()
                return result
            finally:
                # Final update
                live.update(self.get_display_layout())

    def update_display(self) -> None:
        """Update the live display (call this periodically during import)."""
        if hasattr(self, "live"):
            self.live.update(self.get_display_layout())
