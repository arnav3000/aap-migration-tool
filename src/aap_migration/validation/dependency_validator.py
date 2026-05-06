"""Pre-flight dependency validation for imports."""

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from aap_migration.cli.utils import echo_error, echo_info, echo_success, echo_warning
from aap_migration.migration.state import MigrationState
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class DependencyValidator:
    """Validates resource dependencies before import."""

    def __init__(self, state: MigrationState, input_dir: Path):
        """Initialize dependency validator.

        Args:
            state: Migration state manager
            input_dir: Directory containing transformed data
        """
        self.state = state
        self.input_dir = input_dir
        self.console = Console()

        # Dependency mapping (field -> resource_type)
        self.dependencies = {
            "credentials": {"organization": "organizations", "credential_type": "credential_types"},
            "projects": {"organization": "organizations", "credential": "credentials"},
            "inventories": {"organization": "organizations"},
            "inventory_sources": {
                "inventory": "inventories",
                "source_project": "projects",
                "credential": "credentials",
            },
            "hosts": {"inventory": "inventories"},
            "job_templates": {
                "organization": "organizations",
                "inventory": "inventories",
                "project": "projects",
                "credential": "credentials",
            },
            "workflow_job_templates": {"organization": "organizations"},
            "schedules": {},  # Polymorphic - depends on unified_job_template
        }

    def load_resources(self, resource_type: str) -> list[dict[str, Any]]:
        """Load resources from transformed files.

        Args:
            resource_type: Type of resource to load

        Returns:
            List of resource dictionaries
        """
        resource_dir = self.input_dir / resource_type
        if not resource_dir.exists():
            return []

        all_resources = []
        for file_path in sorted(resource_dir.glob(f"{resource_type}_*.json")):
            try:
                with open(file_path) as f:
                    resources = json.load(f)
                    if isinstance(resources, list):
                        all_resources.extend(resources)
                    else:
                        all_resources.append(resources)
            except Exception as e:
                logger.warning(
                    f"Failed to load {file_path}: {e}",
                    resource_type=resource_type,
                    file=str(file_path),
                )

        return all_resources

    def validate_resource_dependencies(
        self, resource: dict[str, Any], resource_type: str
    ) -> dict[str, Any]:
        """Validate dependencies for a single resource.

        Args:
            resource: Resource data
            resource_type: Type of resource

        Returns:
            Validation result with status and issues
        """
        result = {
            "resource": resource.get("name", "N/A"),
            "source_id": resource.get("_source_id") or resource.get("id"),
            "status": "ready",
            "missing_deps": [],
            "warnings": [],
        }

        # Get dependency fields for this resource type
        dep_fields = self.dependencies.get(resource_type, {})

        for field, dep_type in dep_fields.items():
            dep_id = resource.get(field)
            if not dep_id:
                continue

            # Check if dependency is already imported
            mapping = self.state.get_mapped_id(dep_type, dep_id)

            if not mapping:
                # Dependency not yet imported
                result["missing_deps"].append({
                    "field": field,
                    "type": dep_type,
                    "source_id": dep_id,
                })
                result["status"] = "blocked"

        return result

    def validate_batch(self, resource_type: str) -> dict[str, Any]:
        """Validate all resources of a given type.

        Args:
            resource_type: Type of resources to validate

        Returns:
            Validation summary with counts and issues
        """
        resources = self.load_resources(resource_type)

        if not resources:
            return {
                "resource_type": resource_type,
                "total": 0,
                "ready": 0,
                "blocked": 0,
                "warnings": 0,
                "issues": [],
            }

        ready_count = 0
        blocked_count = 0
        warning_count = 0
        issues = []

        for resource in resources:
            validation = self.validate_resource_dependencies(resource, resource_type)

            if validation["status"] == "ready":
                ready_count += 1
            elif validation["status"] == "blocked":
                blocked_count += 1
                issues.append(validation)
            elif validation["warnings"]:
                warning_count += 1
                issues.append(validation)

        return {
            "resource_type": resource_type,
            "total": len(resources),
            "ready": ready_count,
            "blocked": blocked_count,
            "warnings": warning_count,
            "issues": issues,
        }

    def validate_all(self, resource_types: list[str] | None = None) -> dict[str, Any]:
        """Validate all resources or specific types.

        Args:
            resource_types: List of resource types to validate, or None for all

        Returns:
            Overall validation summary
        """
        if not resource_types:
            resource_types = list(self.dependencies.keys())

        results = []
        overall_ready = 0
        overall_blocked = 0
        overall_warnings = 0

        for rtype in resource_types:
            batch_result = self.validate_batch(rtype)
            results.append(batch_result)
            overall_ready += batch_result["ready"]
            overall_blocked += batch_result["blocked"]
            overall_warnings += batch_result["warnings"]

        return {
            "results": results,
            "overall": {
                "ready": overall_ready,
                "blocked": overall_blocked,
                "warnings": overall_warnings,
                "can_proceed": overall_blocked == 0,
            },
        }

    def display_validation_report(self, validation: dict[str, Any]) -> None:
        """Display validation results in a formatted table.

        Args:
            validation: Validation results from validate_all()
        """
        self.console.print("\n[bold cyan]Pre-Flight Dependency Validation[/bold cyan]\n")

        # Summary table
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Resource Type", style="cyan", width=25)
        table.add_column("Total", justify="right", width=8)
        table.add_column("Ready", justify="right", width=8, style="green")
        table.add_column("Blocked", justify="right", width=8, style="red")
        table.add_column("Warnings", justify="right", width=10, style="yellow")
        table.add_column("Status", width=15)

        for result in validation["results"]:
            if result["total"] == 0:
                continue

            if result["blocked"] > 0:
                status = "[red]✗ BLOCKED[/red]"
            elif result["warnings"] > 0:
                status = "[yellow]⚠ WARNING[/yellow]"
            else:
                status = "[green]✓ READY[/green]"

            table.add_row(
                result["resource_type"],
                str(result["total"]),
                str(result["ready"]),
                str(result["blocked"]) if result["blocked"] > 0 else "-",
                str(result["warnings"]) if result["warnings"] > 0 else "-",
                status,
            )

        self.console.print(table)
        self.console.print()

        # Overall summary
        overall = validation["overall"]
        self.console.print("[bold]Overall Summary:[/bold]")
        self.console.print(f"  [green]✓ Ready to import: {overall['ready']}[/green]")
        if overall["blocked"] > 0:
            self.console.print(f"  [red]✗ Blocked: {overall['blocked']}[/red]")
        if overall["warnings"] > 0:
            self.console.print(f"  [yellow]⚠ Warnings: {overall['warnings']}[/yellow]")

        self.console.print()

        # Decision
        if overall["can_proceed"]:
            self.console.print("[bold green]✓ All dependencies satisfied - safe to proceed![/bold green]")
        else:
            self.console.print("[bold red]✗ Cannot proceed - fix dependency issues first[/bold red]")

        self.console.print()

        # Show detailed issues if any
        if overall["blocked"] > 0 or overall["warnings"] > 0:
            self.console.print("[bold yellow]Detailed Issues:[/bold yellow]\n")

            for result in validation["results"]:
                if not result["issues"]:
                    continue

                tree = Tree(f"[cyan]{result['resource_type']}[/cyan]")

                for issue in result["issues"][:5]:  # Limit to first 5
                    resource_name = issue["resource"]
                    source_id = issue["source_id"]
                    node = tree.add(f"[yellow]{resource_name}[/yellow] (source ID: {source_id})")

                    for dep in issue["missing_deps"]:
                        node.add(
                            f"[red]✗[/red] Missing {dep['type']}: source ID {dep['source_id']}"
                        )

                if len(result["issues"]) > 5:
                    tree.add(f"[dim]... and {len(result['issues']) - 5} more[/dim]")

                self.console.print(tree)
                self.console.print()
