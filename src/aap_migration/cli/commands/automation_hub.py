"""
Automation Hub migration commands.

This module provides commands for migrating Automation Hub content
(collections, namespaces, repositories) from AAP 2.4 to AAP 2.6.
"""

import asyncio
import json
from pathlib import Path
from typing import Optional

import click

from aap_migration.automation_hub import (
    AutomationHubExporter,
    AutomationHubImporter,
    AutomationHubTransformer,
)
from aap_migration.cli.decorators import handle_errors, pass_context, requires_config
from aap_migration.cli.utils import (
    echo_error,
    echo_info,
    echo_success,
    echo_warning,
    print_table,
)
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


@click.group(name="hub")
def hub():
    """Automation Hub migration commands.

    Migrate collections, namespaces, and repositories from source
    Automation Hub (AAP 2.4) to target (AAP 2.6).
    """
    pass


@hub.command(name="export")
@pass_context
@requires_config
@handle_errors
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Export directory (default: exports/)",
)
@click.option(
    "--no-artifacts",
    is_flag=True,
    help="Skip downloading collection artifacts (faster, but can't import later)",
)
def export_hub(ctx, output: Path | None, no_artifacts: bool):
    """Export Automation Hub content from source.

    Exports:
    - Namespaces (organizational units)
    - Collections (all versions)
    - Collection artifacts (.tar.gz files)
    - Repositories
    - Remote registries

    Examples:

        # Export with artifacts
        aap-bridge hub export

        # Export without artifacts (metadata only)
        aap-bridge hub export --no-artifacts

        # Custom output directory
        aap-bridge hub export --output /tmp/hub-export
    """
    export_dir = output or Path("exports")

    # Validate configuration
    if not ctx.config.automation_hub:
        echo_error("No automation_hub configuration found in config file")
        raise click.Abort()

    hub_config = ctx.config.automation_hub
    source_config = hub_config.source

    source_url = source_config.url
    source_token = source_config.token
    source_username = source_config.username
    source_password = source_config.password

    # Validate authentication credentials
    if not source_url:
        echo_error("Source Automation Hub URL required in config")
        raise click.Abort()

    if not source_token and not (source_username and source_password):
        echo_error("Source authentication required: either token OR username/password")
        echo_info("Add to config.yaml:")
        echo_info("  automation_hub:")
        echo_info("    source:")
        echo_info("      url: https://source-hub:10443")
        echo_info("      # AAP 2.6 - Use token:")
        echo_info("      token: ${SOURCE_HUB_TOKEN}")
        echo_info("      # AAP 2.4 - Use username/password:")
        echo_info("      # username: ${SOURCE_HUB_USERNAME}")
        echo_info("      # password: ${SOURCE_HUB_PASSWORD}")
        raise click.Abort()

    download_artifacts = not no_artifacts

    echo_info(f"Exporting Automation Hub from: {source_url}")
    echo_info(f"Export directory: {export_dir}")
    echo_info(f"Download artifacts: {'Yes' if download_artifacts else 'No'}")
    echo_info("")

    try:
        # Run async export
        asyncio.run(
            _run_export(
                source_url=source_url,
                source_token=source_token,
                source_username=source_username,
                source_password=source_password,
                export_dir=export_dir,
                download_artifacts=download_artifacts,
                verify_ssl=hub_config.verify_ssl,
            )
        )

        echo_success("✓ Export completed successfully")
        echo_info(f"  Exported to: {export_dir / 'automation_hub'}")

        # Show summary if available
        summary_file = export_dir / "automation_hub" / "export_summary.json"
        if summary_file.exists():
            with open(summary_file) as f:
                summary = json.load(f)

            echo_info("")
            echo_info("Export Summary:")
            counts = summary.get("counts", {})
            echo_info(f"  Namespaces:   {counts.get('namespaces', 0)}")
            echo_info(f"  Collections:  {counts.get('collections', 0)}")
            echo_info(f"  Repositories: {counts.get('repositories', 0)}")
            echo_info(f"  Remotes:      {counts.get('remotes', 0)}")

    except Exception as e:
        echo_error(f"Export failed: {e}")
        raise


@hub.command(name="import")
@pass_context
@requires_config
@handle_errors
@click.option(
    "--input",
    "-i",
    type=click.Path(exists=True, path_type=Path),
    help="Export directory to import from (default: exports/)",
)
@click.option(
    "--skip-existing/--no-skip-existing",
    default=True,
    help="Skip resources that already exist on target (default: skip)",
)
@click.option(
    "--no-artifacts",
    is_flag=True,
    help="Skip uploading collection artifacts",
)
def import_hub(ctx, input: Path | None, skip_existing: bool, no_artifacts: bool):
    """Import Automation Hub content to target.

    Imports:
    - Namespaces
    - Repositories
    - Remote registries
    - Collections (uploaded as artifacts)

    Prerequisites:
    - Export must be completed first
    - Target Automation Hub must be accessible

    Examples:

        # Import from default export directory
        aap-bridge hub import

        # Import from custom directory
        aap-bridge hub import --input /tmp/hub-export

        # Overwrite existing resources
        aap-bridge hub import --no-skip-existing

        # Import metadata only (no artifacts)
        aap-bridge hub import --no-artifacts
    """
    export_dir = input or Path("exports")
    hub_export_dir = export_dir / "automation_hub"

    if not hub_export_dir.exists():
        echo_error(f"No Automation Hub export found at {hub_export_dir}")
        echo_info("Run 'aap-bridge hub export' first")
        raise click.Abort()

    # Validate configuration
    if not ctx.config.automation_hub:
        echo_error("No automation_hub configuration found in config file")
        raise click.Abort()

    hub_config = ctx.config.automation_hub
    target_config = hub_config.target

    target_url = target_config.url
    target_token = target_config.token
    target_username = target_config.username
    target_password = target_config.password

    # Validate authentication credentials
    if not target_url:
        echo_error("Target Automation Hub URL required in config")
        raise click.Abort()

    if not target_token and not (target_username and target_password):
        echo_error("Target authentication required: either token OR username/password")
        echo_info("Add to config.yaml:")
        echo_info("  automation_hub:")
        echo_info("    target:")
        echo_info("      url: https://target-hub:10443")
        echo_info("      # AAP 2.6 - Use token:")
        echo_info("      token: ${TARGET_HUB_TOKEN}")
        echo_info("      # AAP 2.4 - Use username/password:")
        echo_info("      # username: ${TARGET_HUB_USERNAME}")
        echo_info("      # password: ${TARGET_HUB_PASSWORD}")
        raise click.Abort()

    upload_artifacts = not no_artifacts

    echo_info(f"Importing Automation Hub to: {target_url}")
    echo_info(f"From directory: {export_dir}")
    echo_info(f"Skip existing: {'Yes' if skip_existing else 'No'}")
    echo_info(f"Upload artifacts: {'Yes' if upload_artifacts else 'No'}")
    echo_info("")

    try:
        # Run async import
        stats = asyncio.run(
            _run_import(
                target_url=target_url,
                target_token=target_token,
                target_username=target_username,
                target_password=target_password,
                export_dir=export_dir,
                skip_existing=skip_existing,
                upload_artifacts=upload_artifacts,
                verify_ssl=hub_config.verify_ssl,
            )
        )

        echo_success("✓ Import completed successfully")
        echo_info("")

        # Display statistics
        _display_import_stats(stats)

    except Exception as e:
        echo_error(f"Import failed: {e}")
        raise


@hub.command(name="migrate")
@pass_context
@requires_config
@handle_errors
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Export directory (default: exports/)",
)
@click.option(
    "--no-artifacts",
    is_flag=True,
    help="Skip downloading/uploading collection artifacts",
)
@click.option(
    "--skip-existing/--no-skip-existing",
    default=True,
    help="Skip resources that already exist on target (default: skip)",
)
def migrate_hub(ctx, output: Path | None, no_artifacts: bool, skip_existing: bool):
    """Complete Automation Hub migration (export + import).

    Performs full migration workflow:
    1. Export from source Automation Hub
    2. Import to target Automation Hub

    This is equivalent to running:
        aap-bridge hub export
        aap-bridge hub import

    Examples:

        # Full migration with artifacts
        aap-bridge hub migrate

        # Metadata-only migration (no artifacts)
        aap-bridge hub migrate --no-artifacts

        # Custom export directory
        aap-bridge hub migrate --output /tmp/hub-migration
    """
    export_dir = output or Path("exports")

    echo_info("=" * 60)
    echo_info("Automation Hub Migration")
    echo_info("=" * 60)
    echo_info("")

    # Step 1: Export
    echo_info("Step 1: Exporting from source...")
    echo_info("-" * 60)

    ctx.invoke(export_hub, output=export_dir, no_artifacts=no_artifacts)

    echo_info("")
    echo_info("")

    # Step 2: Import
    echo_info("Step 2: Importing to target...")
    echo_info("-" * 60)

    ctx.invoke(
        import_hub,
        input=export_dir,
        skip_existing=skip_existing,
        no_artifacts=no_artifacts,
    )

    echo_info("")
    echo_info("=" * 60)
    echo_success("Migration completed")
    echo_info("=" * 60)


@hub.command(name="status")
@pass_context
@handle_errors
@click.option(
    "--input",
    "-i",
    type=click.Path(exists=True, path_type=Path),
    help="Export directory (default: exports/)",
)
def hub_status(ctx, input: Path | None):
    """Show Automation Hub export/import status.

    Displays:
    - Export summary
    - Transformation statistics
    - Import statistics (if available)

    Examples:

        # Show status from default directory
        aap-bridge hub status

        # Show status from custom directory
        aap-bridge hub status --input /tmp/hub-export
    """
    export_dir = input or Path("exports")
    hub_dir = export_dir / "automation_hub"

    if not hub_dir.exists():
        echo_warning(f"No Automation Hub export found at {hub_dir}")
        return

    # Show export summary
    summary_file = hub_dir / "export_summary.json"
    if summary_file.exists():
        with open(summary_file) as f:
            summary = json.load(f)

        echo_info("Export Summary")
        echo_info("=" * 60)
        echo_info(f"Source URL: {summary.get('source_url', 'N/A')}")
        echo_info(f"Export directory: {summary.get('export_dir', 'N/A')}")
        echo_info(f"Artifacts downloaded: {summary.get('artifacts_downloaded', 'N/A')}")
        echo_info("")

        counts = summary.get("counts", {})
        data = [
            ["Namespaces", counts.get("namespaces", 0)],
            ["Collections", counts.get("collections", 0)],
            ["Repositories", counts.get("repositories", 0)],
            ["Remotes", counts.get("remotes", 0)],
        ]
        print_table("Exported Resources", ["Resource Type", "Count"], data)
        echo_info("")

    # Show index files if available
    namespaces_index = hub_dir / "namespaces" / "_index.json"
    if namespaces_index.exists():
        with open(namespaces_index) as f:
            ns_data = json.load(f)

        echo_info("Namespaces")
        echo_info("-" * 60)
        for ns in ns_data.get("namespaces", [])[:10]:  # Show first 10
            echo_info(f"  • {ns['name']}")
            if ns.get("company"):
                echo_info(f"    Company: {ns['company']}")

        if len(ns_data.get("namespaces", [])) > 10:
            echo_info(f"  ... and {len(ns_data['namespaces']) - 10} more")
        echo_info("")

    collections_index = hub_dir / "collections" / "_index.json"
    if collections_index.exists():
        with open(collections_index) as f:
            coll_data = json.load(f)

        echo_info("Collections")
        echo_info("-" * 60)
        for coll in coll_data.get("items", [])[:10]:  # Show first 10
            echo_info(f"  • {coll['fqn']}")
            echo_info(f"    Versions: {', '.join(coll['versions'][:5])}")
            if len(coll["versions"]) > 5:
                echo_info(f"    ... and {len(coll['versions']) - 5} more versions")

        if len(coll_data.get("items", [])) > 10:
            echo_info(f"  ... and {len(coll_data['items']) - 10} more collections")


# =========================================================================
# Helper Functions
# =========================================================================


async def _run_export(
    source_url: str,
    source_token: Optional[str],
    source_username: Optional[str],
    source_password: Optional[str],
    export_dir: Path,
    download_artifacts: bool,
    verify_ssl: bool,
):
    """Run async export operation."""
    exporter = AutomationHubExporter(
        source_url=source_url,
        export_dir=export_dir,
        source_token=source_token,
        source_username=source_username,
        source_password=source_password,
        verify_ssl=verify_ssl,
        download_artifacts=download_artifacts,
    )

    await exporter.export_all()


async def _run_import(
    target_url: str,
    target_token: Optional[str],
    target_username: Optional[str],
    target_password: Optional[str],
    export_dir: Path,
    skip_existing: bool,
    upload_artifacts: bool,
    verify_ssl: bool,
) -> dict:
    """Run async import operation.

    Returns:
        Import statistics dictionary
    """
    importer = AutomationHubImporter(
        target_url=target_url,
        export_dir=export_dir,
        target_token=target_token,
        target_username=target_username,
        target_password=target_password,
        verify_ssl=verify_ssl,
        skip_existing=skip_existing,
        upload_artifacts=upload_artifacts,
    )

    await importer.import_all()

    return importer.get_import_stats()


def _display_import_stats(stats: dict):
    """Display import statistics in table format."""
    echo_info("Import Statistics")
    echo_info("=" * 60)

    # Namespaces
    data = [
        ["Created", stats["namespaces"]["created"]],
        ["Skipped", stats["namespaces"]["skipped"]],
        ["Failed", stats["namespaces"]["failed"]],
        ["Total", stats["namespaces"]["total"]],
    ]
    print_table("Namespaces", ["Action", "Count"], data)
    echo_info("")

    # Collections
    data = [
        ["Uploaded", stats["collections"]["uploaded"]],
        ["Skipped", stats["collections"]["skipped"]],
        ["Failed", stats["collections"]["failed"]],
        ["Total", stats["collections"]["total"]],
    ]
    print_table("Collections", ["Action", "Count"], data)
    echo_info("")

    # Repositories
    data = [
        ["Created", stats["repositories"]["created"]],
        ["Skipped", stats["repositories"]["skipped"]],
        ["Failed", stats["repositories"]["failed"]],
        ["Total", stats["repositories"]["total"]],
    ]
    print_table("Repositories", ["Action", "Count"], data)
    echo_info("")

    # Remotes
    data = [
        ["Created", stats["remotes"]["created"]],
        ["Skipped", stats["remotes"]["skipped"]],
        ["Failed", stats["remotes"]["failed"]],
        ["Total", stats["remotes"]["total"]],
    ]
    print_table("Remotes", ["Action", "Count"], data)
    echo_info("")

    # Container Repositories
    data = [
        ["Created", stats["container_repositories"]["created"]],
        ["Skipped", stats["container_repositories"]["skipped"]],
        ["Failed", stats["container_repositories"]["failed"]],
        ["Total", stats["container_repositories"]["total"]],
    ]
    print_table("Container Repositories", ["Action", "Count"], data)
    echo_info("")

    # Container Remotes
    data = [
        ["Created", stats["container_remotes"]["created"]],
        ["Skipped", stats["container_remotes"]["skipped"]],
        ["Failed", stats["container_remotes"]["failed"]],
        ["Total", stats["container_remotes"]["total"]],
    ]
    print_table("Container Remotes", ["Action", "Count"], data)
    echo_info("")

    # Execution Environments
    data = [
        ["Created", stats["execution_environments"]["created"]],
        ["Skipped", stats["execution_environments"]["skipped"]],
        ["Failed", stats["execution_environments"]["failed"]],
        ["Total", stats["execution_environments"]["total"]],
    ]
    print_table("Execution Environments", ["Action", "Count"], data)
    echo_info("")

    # Add note about EE image pushing
    if stats["execution_environments"]["created"] > 0:
        echo_warning("NOTE: EE repository structures created.")
        echo_warning("   Image layers must be pushed separately:")
        echo_warning("   podman push <source-image> <target-hub>/<namespace>/<name>:<tag>")
