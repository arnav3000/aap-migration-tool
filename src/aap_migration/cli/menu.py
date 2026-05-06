import subprocess
import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from aap_migration.cli.import_menu import import_submenu


def run_command(args: list[str], ctx: Any = None) -> None:
    """Run a CLI command in a subprocess."""
    # Use the same executable entry point
    cmd = [sys.argv[0]]

    # Pass config file if present in context - insert BEFORE subcommand args
    if ctx and ctx.obj and ctx.obj.config_path:
        cmd.extend(["--config", str(ctx.obj.config_path)])

    # Add subcommand and its args
    cmd.extend(args)

    try:
        subprocess.run(cmd, check=False)
    except Exception as e:
        print(f"Error running command: {e}")


def interactive_menu(ctx: Any) -> None:
    """Display interactive menu for AAP Bridge."""
    console = Console()

    while True:
        console.clear()
        console.print(
            Panel.fit(
                "[bold cyan]AAP Bridge - Migration Tool[/bold cyan]\n\n"
                "1. Prep Phase (Discover & Schema)\n"
                "2. Export (All)\n"
                "3. Transform (All)\n"
                "4. Import Resources (Enhanced)\n"
                "5. Cleanup\n"
                "q. Quit",
                title="Main Menu",
                border_style="blue",
            )
        )

        choice = Prompt.ask(
            "Select an option", choices=["1", "2", "3", "4", "5", "q"], default="q"
        )

        if choice.lower() == "q":
            break

        console.print()  # Spacer

        if choice == "1":
            run_command(["prep"], ctx)
            Prompt.ask("\nPress Enter to return to menu...")
        elif choice == "2":
            run_command(["export"], ctx)
            Prompt.ask("\nPress Enter to return to menu...")
        elif choice == "3":
            run_command(["transform"], ctx)
            Prompt.ask("\nPress Enter to return to menu...")
        elif choice == "4":
            # Launch enhanced import submenu
            import_submenu(ctx)
        elif choice == "5":
            run_command(["cleanup"], ctx)
            Prompt.ask("\nPress Enter to return to menu...")
