#!/usr/bin/env python3
"""Update AAP 2.6 credentials with real secret values from AAP 2.4 extraction.

This script:
1. Reads decrypted credentials from AAP 2.4 (credentials_decrypted.json)
2. Reads migration state database to get source → target ID mappings
3. Updates target credentials with real secret values
4. Generates detailed report

Usage:
    python3 update_credentials.py --config config.yml --credentials credentials_decrypted.json
    python3 update_credentials.py --config config.yml --credentials credentials_decrypted.json --dry-run
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Add src to path
repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root / "src"))

# Load environment variables from .env file (if dotenv is available)
try:
    from dotenv import load_dotenv
    load_dotenv(repo_root / ".env")
except ImportError:
    # dotenv not installed, environment variables should be set manually
    pass

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.config import load_config_from_yaml
from aap_migration.migration.state import MigrationState
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class CredentialUpdater:
    """Updates AAP 2.6 credentials with real secret values."""

    def __init__(
        self,
        target_client: AAPTargetClient,
        state: MigrationState,
        dry_run: bool = False,
    ):
        """Initialize credential updater.

        Args:
            target_client: AAP 2.6 client
            state: Migration state with ID mappings
            dry_run: If True, don't make actual changes
        """
        self.target_client = target_client
        self.state = state
        self.dry_run = dry_run

        self.stats = {
            "total_credentials": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "no_mapping": 0,
            "no_secrets": 0,
            "managed_skipped": 0,
        }

        self.results = []

    def load_decrypted_credentials(self, file_path: str) -> dict[str, Any]:
        """Load and validate decrypted credentials file.

        Args:
            file_path: Path to credentials_decrypted.json

        Returns:
            Parsed JSON data

        Raises:
            FileNotFoundError: If file doesn't exist
            json.JSONDecodeError: If file is not valid JSON
            ValueError: If file format is invalid
        """
        path = Path(file_path)
        if not path.exists():
            # Try to provide helpful suggestions
            suggestions = []

            # Check if file exists in scripts directory
            scripts_path = Path("scripts") / Path(file_path).name
            if scripts_path.exists():
                suggestions.append(f"  - File found in scripts/: use --credentials {scripts_path}")

            # Check if file exists in parent directory
            parent_path = Path("..") / Path(file_path).name
            if parent_path.exists():
                suggestions.append(f"  - File found in parent dir: use --credentials {parent_path}")

            error_msg = f"Credentials file not found: {file_path}\n"
            if suggestions:
                error_msg += "\nSuggestions:\n" + "\n".join(suggestions)
            else:
                error_msg += f"\nSearched in: {path.absolute()}"

            raise FileNotFoundError(error_msg)

        logger.info("loading_decrypted_credentials", file_path=file_path)

        with open(path, "r") as f:
            data = json.load(f)

        # Validate structure
        if "metadata" not in data or "credentials" not in data:
            raise ValueError("Invalid credentials file format - missing metadata or credentials")

        if not isinstance(data["credentials"], list):
            raise ValueError("Invalid credentials file format - credentials must be a list")

        logger.info(
            "credentials_loaded",
            total_count=len(data["credentials"]),
            extraction_date=data["metadata"].get("extraction_date"),
        )

        return data

    def extract_secret_inputs(self, credential: dict[str, Any]) -> dict[str, Any]:
        """Extract only secret fields from credential inputs.

        This filters out non-secret fields and returns only the fields
        that contain actual secret values (not metadata like username, etc.).

        Args:
            credential: Credential data from decrypted file

        Returns:
            Dictionary with only secret inputs
        """
        inputs = credential.get("inputs", {})
        if not inputs:
            return {}

        # Common secret field names across credential types
        # These are the fields that typically contain encrypted values
        secret_field_patterns = [
            "password",
            "ssh_key_data",
            "ssh_key_unlock",
            "vault_password",
            "secret",
            "token",
            "api_key",
            "private_key",
            "become_password",
            "security_token",
            "client_secret",
            "subscription_key",
            "tenant_password",
        ]

        secret_inputs = {}
        for key, value in inputs.items():
            # Include field if:
            # 1. Key matches known secret patterns
            # 2. Value is not empty/None
            # 3. Value is not a placeholder like "$encrypted$"
            if value and value != "$encrypted$":
                key_lower = key.lower()
                if any(pattern in key_lower for pattern in secret_field_patterns):
                    secret_inputs[key] = value

        return secret_inputs

    async def update_credential(
        self,
        source_id: int,
        credential_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a single credential with real secret values.

        Args:
            source_id: Source credential ID (from AAP 2.4)
            credential_data: Credential data from decrypted file

        Returns:
            Result dictionary with status
        """
        name = credential_data.get("name", "Unknown")
        result = {
            "source_id": source_id,
            "name": name,
            "status": "unknown",
            "message": "",
        }

        logger.info("processing_credential", source_id=source_id, name=name)

        # Skip managed credentials
        if credential_data.get("managed", False):
            result["status"] = "skipped"
            result["message"] = "Managed credential (system-created)"
            self.stats["managed_skipped"] += 1
            logger.debug("skipping_managed_credential", name=name, source_id=source_id)
            return result

        # Get target credential ID from migration state
        target_id = self.state.get_mapped_id("credentials", source_id)
        if not target_id:
            result["status"] = "no_mapping"
            result["message"] = "No mapping found in migration state (credential may not have been migrated)"
            self.stats["no_mapping"] += 1
            logger.warning("credential_no_mapping", source_id=source_id, name=name)
            return result

        result["target_id"] = target_id

        # Extract secret inputs
        secret_inputs = self.extract_secret_inputs(credential_data)
        if not secret_inputs:
            result["status"] = "no_secrets"
            result["message"] = "No secret fields to update"
            self.stats["no_secrets"] += 1
            logger.debug("credential_no_secrets", source_id=source_id, name=name)
            return result

        result["secret_fields"] = list(secret_inputs.keys())

        # Prepare PATCH payload
        patch_data = {"inputs": secret_inputs}

        if self.dry_run:
            result["status"] = "dry_run"
            result["message"] = f"Would update {len(secret_inputs)} secret fields"
            self.stats["updated"] += 1
            logger.info(
                "credential_dry_run",
                name=name,
                source_id=source_id,
                target_id=target_id,
                secret_fields=list(secret_inputs.keys()),
            )
            return result

        # Update credential
        try:
            await self.target_client.patch(
                f"credentials/{target_id}/",
                json_data=patch_data,
            )

            result["status"] = "updated"
            result["message"] = f"Updated {len(secret_inputs)} secret fields"
            self.stats["updated"] += 1

            logger.info(
                "credential_updated",
                name=name,
                source_id=source_id,
                target_id=target_id,
                secret_fields=list(secret_inputs.keys()),
            )

        except Exception as e:
            result["status"] = "failed"
            result["message"] = f"Update failed: {str(e)}"
            result["error"] = str(e)
            self.stats["failed"] += 1

            logger.error(
                "credential_update_failed",
                name=name,
                source_id=source_id,
                target_id=target_id,
                error=str(e),
            )

        return result

    async def update_all_credentials(
        self,
        decrypted_data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Update all credentials with real secret values.

        Args:
            decrypted_data: Loaded credentials_decrypted.json data

        Returns:
            List of result dictionaries
        """
        credentials = decrypted_data["credentials"]
        self.stats["total_credentials"] = len(credentials)

        logger.info(
            "credential_update_starting",
            total_credentials=len(credentials),
            dry_run=self.dry_run,
        )

        results = []
        for credential in credentials:
            source_id = credential.get("id")
            if not source_id:
                logger.warning("credential_missing_id", credential_name=credential.get("name"))
                continue

            result = await self.update_credential(source_id, credential)
            results.append(result)

            # Progress indicator
            processed = len(results)
            if processed % 10 == 0 or processed == len(credentials):
                logger.info(
                    "progress",
                    processed=processed,
                    total=len(credentials),
                    updated=self.stats["updated"],
                    failed=self.stats["failed"],
                )

        logger.info("credential_update_completed", stats=self.stats)

        return results

    def generate_report(self, results: list[dict[str, Any]]) -> str:
        """Generate detailed update report.

        Args:
            results: List of update results

        Returns:
            Markdown formatted report
        """
        report_lines = [
            "# Credential Secret Update Report",
            "",
            f"**Generated:** {datetime.now().isoformat()}",
            f"**Mode:** {'DRY RUN' if self.dry_run else 'LIVE UPDATE'}",
            "",
            "## Summary",
            "",
            f"- **Total Credentials:** {self.stats['total_credentials']}",
            f"- **Updated:** {self.stats['updated']}",
            f"- **Failed:** {self.stats['failed']}",
            f"- **No Mapping:** {self.stats['no_mapping']}",
            f"- **No Secrets:** {self.stats['no_secrets']}",
            f"- **Managed (Skipped):** {self.stats['managed_skipped']}",
            "",
            "---",
            "",
        ]

        # Updated credentials
        updated = [r for r in results if r["status"] in ("updated", "dry_run")]
        if updated:
            report_lines.extend([
                f"## {'Would Update' if self.dry_run else 'Updated'} Credentials ({len(updated)})",
                "",
                "| Source ID | Target ID | Name | Secret Fields | Status |",
                "|-----------|-----------|------|---------------|--------|",
            ])
            for r in updated:
                fields = ", ".join(r.get("secret_fields", []))
                report_lines.append(
                    f"| {r['source_id']} | {r.get('target_id', 'N/A')} | {r['name'][:40]} | {fields} | {r['message']} |"
                )
            report_lines.append("")

        # Failed updates
        failed = [r for r in results if r["status"] == "failed"]
        if failed:
            report_lines.extend([
                f"## Failed Updates ({len(failed)})",
                "",
                "| Source ID | Target ID | Name | Error |",
                "|-----------|-----------|------|-------|",
            ])
            for r in failed:
                error = r.get("error", r.get("message", "Unknown"))[:60]
                report_lines.append(
                    f"| {r['source_id']} | {r.get('target_id', 'N/A')} | {r['name'][:40]} | {error} |"
                )
            report_lines.append("")

        # No mapping
        no_mapping = [r for r in results if r["status"] == "no_mapping"]
        if no_mapping:
            report_lines.extend([
                f"## No Mapping Found ({len(no_mapping)})",
                "",
                "These credentials were not migrated or have no ID mapping in migration state.",
                "",
                "| Source ID | Name | Message |",
                "|-----------|------|---------|",
            ])
            for r in no_mapping:
                report_lines.append(
                    f"| {r['source_id']} | {r['name'][:40]} | {r['message']} |"
                )
            report_lines.append("")

        # No secrets
        no_secrets = [r for r in results if r["status"] == "no_secrets"]
        if no_secrets:
            report_lines.extend([
                f"## No Secrets to Update ({len(no_secrets)})",
                "",
                "These credentials have no secret fields or all fields were empty.",
                "",
                f"Count: {len(no_secrets)} credentials",
                "",
            ])

        report_lines.extend([
            "---",
            "",
            "## Next Steps",
            "",
        ])

        if self.dry_run:
            report_lines.extend([
                "This was a **DRY RUN** - no changes were made.",
                "",
                "To apply changes:",
                "```bash",
                "python3 update_credentials.py --config config.yml --credentials credentials_decrypted.json",
                "```",
                "",
            ])
        else:
            report_lines.extend([
                "Credentials have been updated with real secret values.",
                "",
                "**IMPORTANT:** Securely delete the decrypted file:",
                "```bash",
                "shred -u credentials_decrypted.json",
                "```",
                "",
            ])

        return "\n".join(report_lines)


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Update AAP 2.6 credentials with real secret values from AAP 2.4"
    )
    parser.add_argument(
        "--config",
        "-c",
        required=True,
        help="Path to migration config file (config.yml)",
    )
    parser.add_argument(
        "--credentials",
        required=True,
        help="Path to decrypted credentials file (credentials_decrypted.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform a dry run without making changes",
    )
    parser.add_argument(
        "--report",
        "-r",
        default="./credential-update-report.md",
        help="Path for output report (default: ./credential-update-report.md)",
    )

    args = parser.parse_args()

    print("=" * 70)
    print("AAP Credential Secret Update")
    print("=" * 70)
    print(f"Config: {args.config}")
    print(f"Credentials: {args.credentials}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE UPDATE'}")
    print(f"Report: {args.report}")
    print("=" * 70)
    print()

    try:
        # Load config
        print("Loading configuration...")
        config = load_config_from_yaml(args.config)

        # Initialize migration state
        print("Connecting to migration state database...")
        state = MigrationState(config.state)

        # Initialize target client
        print("Connecting to AAP 2.6...")
        target_client = AAPTargetClient(
            config=config.target,
        )

        # Load decrypted credentials
        print("Loading decrypted credentials...")
        updater = CredentialUpdater(
            target_client=target_client,
            state=state,
            dry_run=args.dry_run,
        )

        decrypted_data = updater.load_decrypted_credentials(args.credentials)
        print(f"✓ Loaded {len(decrypted_data['credentials'])} credentials")
        print()

        # Update credentials
        if args.dry_run:
            print("DRY RUN MODE - No changes will be made")
        print("Updating credentials...")
        print()

        results = await updater.update_all_credentials(decrypted_data)

        # Generate report
        print()
        print("Generating report...")
        report = updater.generate_report(results)

        # Save report
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w") as f:
            f.write(report)

        print(f"✓ Report saved: {args.report}")
        print()

        # Display summary
        print("=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"Total Credentials:     {updater.stats['total_credentials']}")
        print(f"Updated:               {updater.stats['updated']}")
        print(f"Failed:                {updater.stats['failed']}")
        print(f"No Mapping:            {updater.stats['no_mapping']}")
        print(f"No Secrets:            {updater.stats['no_secrets']}")
        print(f"Managed (Skipped):     {updater.stats['managed_skipped']}")
        print("=" * 70)

        if args.dry_run:
            print()
            print("This was a DRY RUN - no changes were made.")
            print("Review the report and run without --dry-run to apply changes.")
        else:
            print()
            if updater.stats["failed"] > 0:
                print("⚠️  Some credentials failed to update. Check the report for details.")
                return 1
            else:
                print("✓ All credentials updated successfully!")
                print()
                print("IMPORTANT: Securely delete the decrypted file:")
                print(f"  shred -u {args.credentials}")

        return 0

    except Exception as e:
        print(f"\n✗ ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
