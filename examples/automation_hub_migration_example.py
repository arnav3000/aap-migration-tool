#!/usr/bin/env python3
"""
Example: Complete Automation Hub Migration

Demonstrates the full Export-Transform-Import pipeline for migrating
Automation Hub content from AAP 2.4 to AAP 2.6.

Usage:
    # Export from source
    python examples/automation_hub_migration_example.py export

    # Import to target
    python examples/automation_hub_migration_example.py import

    # Full migration (export + import)
    python examples/automation_hub_migration_example.py migrate

Environment Variables:
    SOURCE_HUB_URL   - Source Automation Hub URL (AAP 2.4)
    SOURCE_HUB_TOKEN - Source authentication token
    TARGET_HUB_URL   - Target Automation Hub URL (AAP 2.6)
    TARGET_HUB_TOKEN - Target authentication token
    EXPORT_DIR       - Directory for exports (default: ./exports)
"""

import asyncio
import os
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aap_migration.automation_hub import (
    AutomationHubExporter,
    AutomationHubTransformer,
    AutomationHubImporter,
)
from aap_migration.db.session import SessionLocal
from aap_migration.models import ExportRun, ImportRun


async def export_hub():
    """Export Automation Hub content from source."""
    source_url = os.getenv("SOURCE_HUB_URL")
    source_token = os.getenv("SOURCE_HUB_TOKEN")
    export_dir = Path(os.getenv("EXPORT_DIR", "./exports"))

    if not source_url or not source_token:
        print("Error: SOURCE_HUB_URL and SOURCE_HUB_TOKEN required")
        return

    print(f"Exporting from: {source_url}")
    print(f"Export directory: {export_dir}")
    print()

    # Create database session and export run
    session = SessionLocal()
    export_run = ExportRun(status="running")
    session.add(export_run)
    session.commit()

    try:
        # Create exporter
        exporter = AutomationHubExporter(
            source_url=source_url,
            source_token=source_token,
            export_dir=export_dir,
            session=session,
            export_run=export_run,
            verify_ssl=False,  # For local testing
            download_artifacts=True,
        )

        # Run export
        await exporter.export_all()

        export_run.status = "completed"
        session.commit()

        print()
        print("✓ Export completed successfully")
        print(f"  Exported to: {export_dir / 'automation_hub'}")

    except Exception as e:
        export_run.status = "failed"
        session.commit()
        print(f"✗ Export failed: {e}")
        raise

    finally:
        session.close()


async def import_hub():
    """Import Automation Hub content to target."""
    target_url = os.getenv("TARGET_HUB_URL")
    target_token = os.getenv("TARGET_HUB_TOKEN")
    export_dir = Path(os.getenv("EXPORT_DIR", "./exports"))

    if not target_url or not target_token:
        print("Error: TARGET_HUB_URL and TARGET_HUB_TOKEN required")
        return

    if not (export_dir / "automation_hub").exists():
        print(f"Error: No export found at {export_dir / 'automation_hub'}")
        print("Run export first: python examples/automation_hub_migration_example.py export")
        return

    print(f"Importing to: {target_url}")
    print(f"From directory: {export_dir}")
    print()

    # Create database session and import run
    session = SessionLocal()
    import_run = ImportRun(status="running")
    session.add(import_run)
    session.commit()

    try:
        # Create importer
        importer = AutomationHubImporter(
            target_url=target_url,
            target_token=target_token,
            export_dir=export_dir,
            session=session,
            import_run=import_run,
            verify_ssl=False,  # For local testing
            skip_existing=True,
            upload_artifacts=True,
        )

        # Run import
        await importer.import_all()

        import_run.status = "completed"
        session.commit()

        # Print statistics
        stats = importer.get_import_stats()
        print()
        print("✓ Import completed successfully")
        print()
        print("Statistics:")
        print(f"  Namespaces:")
        print(f"    Created: {stats['namespaces']['created']}")
        print(f"    Skipped: {stats['namespaces']['skipped']}")
        print(f"    Failed:  {stats['namespaces']['failed']}")
        print()
        print(f"  Collections:")
        print(f"    Uploaded: {stats['collections']['uploaded']}")
        print(f"    Skipped:  {stats['collections']['skipped']}")
        print(f"    Failed:   {stats['collections']['failed']}")
        print()
        print(f"  Repositories:")
        print(f"    Created: {stats['repositories']['created']}")
        print(f"    Skipped: {stats['repositories']['skipped']}")
        print(f"    Failed:  {stats['repositories']['failed']}")
        print()
        print(f"  Remotes:")
        print(f"    Created: {stats['remotes']['created']}")
        print(f"    Skipped: {stats['remotes']['skipped']}")
        print(f"    Failed:  {stats['remotes']['failed']}")

    except Exception as e:
        import_run.status = "failed"
        session.commit()
        print(f"✗ Import failed: {e}")
        raise

    finally:
        session.close()


async def migrate_hub():
    """Perform complete migration (export + import)."""
    print("=" * 60)
    print("Automation Hub Migration")
    print("=" * 60)
    print()

    # Export
    print("Step 1: Exporting from source...")
    print("-" * 60)
    await export_hub()

    print()
    print()

    # Import
    print("Step 2: Importing to target...")
    print("-" * 60)
    await import_hub()

    print()
    print("=" * 60)
    print("Migration completed")
    print("=" * 60)


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print(__doc__)
        return

    command = sys.argv[1]

    if command == "export":
        asyncio.run(export_hub())

    elif command == "import":
        asyncio.run(import_hub())

    elif command == "migrate":
        asyncio.run(migrate_hub())

    else:
        print(f"Unknown command: {command}")
        print()
        print(__doc__)


if __name__ == "__main__":
    main()
