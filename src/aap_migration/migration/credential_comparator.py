"""Credential comparison and diff-based migration.

This module provides functionality to:
1. Fetch credentials from source and target AAP instances
2. Compare and find missing credentials
3. Migrate only missing credentials
4. Generate detailed reports
"""

from dataclasses import dataclass
from typing import Any

from aap_migration.client.aap_source_client import AAPSourceClient
from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.migration.state import MigrationState
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class CredentialDiff:
    """Represents a credential that exists in source but not in target."""

    source_id: int
    name: str
    credential_type: int
    credential_type_name: str
    organization: int | None
    organization_name: str | None
    description: str
    inputs: dict[str, Any]
    managed: bool


@dataclass
class CredentialComparisonResult:
    """Results of credential comparison between source and target."""

    total_source: int
    total_target: int
    missing_in_target: list[CredentialDiff]
    matching_credentials: int
    managed_credentials_skipped: int


class CredentialComparator:
    """Compares credentials between source and target AAP instances."""

    def __init__(
        self,
        source_client: AAPSourceClient,
        target_client: AAPTargetClient,
        state: MigrationState,
    ):
        """Initialize credential comparator.

        Args:
            source_client: Source AAP client
            target_client: Target AAP client
            state: Migration state manager
        """
        self.source_client = source_client
        self.target_client = target_client
        self.state = state

    async def fetch_credentials(self, client: AAPSourceClient | AAPTargetClient) -> list[dict[str, Any]]:
        """Fetch all credentials from an AAP instance.

        Args:
            client: AAP client (source or target)

        Returns:
            List of credential dictionaries
        """
        credentials = []
        page = 1
        page_size = 200

        logger.info("fetching_credentials", client_type=type(client).__name__)

        while True:
            try:
                response = await client.get(
                    "/credentials/",
                    params={"page": page, "page_size": page_size},
                )

                results = response.get("results", [])
                credentials.extend(results)

                logger.debug(
                    "fetched_credential_page",
                    page=page,
                    count=len(results),
                    total_fetched=len(credentials),
                )

                # Check if there are more pages
                if not response.get("next"):
                    break

                page += 1

            except Exception as e:
                logger.error(
                    "credential_fetch_failed",
                    page=page,
                    error=str(e),
                )
                raise

        logger.info(
            "credentials_fetched",
            total_count=len(credentials),
            client_type=type(client).__name__,
        )

        return credentials

    async def compare_credentials(self) -> CredentialComparisonResult:
        """Compare credentials between source and target instances.

        Returns:
            CredentialComparisonResult with diff details
        """
        logger.info("credential_comparison_started")

        # Fetch credentials from both instances
        source_credentials = await self.fetch_credentials(self.source_client)
        target_credentials = await self.fetch_credentials(self.target_client)

        # Build target credential index by (name, credential_type, organization)
        # This uniquely identifies a credential
        target_index = {}
        for cred in target_credentials:
            key = (
                cred.get("name"),
                cred.get("credential_type"),
                cred.get("organization"),  # Can be None
            )
            target_index[key] = cred

        logger.debug(
            "credential_index_built",
            source_count=len(source_credentials),
            target_count=len(target_credentials),
            target_index_size=len(target_index),
        )

        # Find credentials missing in target
        missing_credentials = []
        matching_count = 0
        managed_skipped = 0

        for source_cred in source_credentials:
            # Skip managed credentials (system-created)
            if source_cred.get("managed", False):
                managed_skipped += 1
                logger.debug(
                    "skipping_managed_credential",
                    name=source_cred.get("name"),
                    id=source_cred.get("id"),
                )
                continue

            key = (
                source_cred.get("name"),
                source_cred.get("credential_type"),
                source_cred.get("organization"),
            )

            if key in target_index:
                # Credential exists in target
                matching_count += 1
                target_cred = target_index[key]

                # Store ID mapping if not already stored
                source_id = source_cred.get("id")
                target_id = target_cred.get("id")
                source_name = source_cred.get("name")

                if not self.state.is_migrated("credentials", source_id):
                    self.state.save_id_mapping("credentials", source_id, target_id, source_name=source_name)
                    # Mark as completed to prevent orphaned ID mapping
                    self.state.mark_completed(
                        resource_type="credentials",
                        source_id=source_id,
                        target_id=target_id,
                        target_name=source_name,
                        source_name=source_name,
                    )
                    logger.debug(
                        "credential_mapping_stored",
                        name=source_name,
                        source_id=source_id,
                        target_id=target_id,
                    )
            else:
                # Credential missing in target
                diff = CredentialDiff(
                    source_id=source_cred.get("id"),
                    name=source_cred.get("name"),
                    credential_type=source_cred.get("credential_type"),
                    credential_type_name=source_cred.get("summary_fields", {})
                    .get("credential_type", {})
                    .get("name", "Unknown"),
                    organization=source_cred.get("organization"),
                    organization_name=source_cred.get("summary_fields", {})
                    .get("organization", {})
                    .get("name"),
                    description=source_cred.get("description", ""),
                    inputs=source_cred.get("inputs", {}),
                    managed=source_cred.get("managed", False),
                )
                missing_credentials.append(diff)

        result = CredentialComparisonResult(
            total_source=len(source_credentials),
            total_target=len(target_credentials),
            missing_in_target=missing_credentials,
            matching_credentials=matching_count,
            managed_credentials_skipped=managed_skipped,
        )

        logger.info(
            "credential_comparison_completed",
            total_source=result.total_source,
            total_target=result.total_target,
            missing_count=len(result.missing_in_target),
            matching_count=result.matching_credentials,
            managed_skipped=result.managed_credentials_skipped,
        )

        return result

    def generate_report(self, result: CredentialComparisonResult) -> str:
        """Generate a detailed comparison report.

        Args:
            result: Comparison result

        Returns:
            Formatted report as markdown string
        """
        report_lines = [
            "# Credential Comparison Report",
            "",
            f"**Total Source Credentials:** {result.total_source}",
            f"**Total Target Credentials:** {result.total_target}",
            f"**Matching Credentials:** {result.matching_credentials}",
            f"**Missing in Target:** {len(result.missing_in_target)}",
            f"**Managed Credentials (Skipped):** {result.managed_credentials_skipped}",
            "",
            "---",
            "",
        ]

        if result.missing_in_target:
            report_lines.extend([
                "## Missing Credentials",
                "",
                "The following credentials exist in source but are missing in target:",
                "",
                "| Source ID | Name | Type | Organization | Description |",
                "|-----------|------|------|--------------|-------------|",
            ])

            for diff in result.missing_in_target:
                org_name = diff.organization_name or "None"
                description = diff.description[:50] if diff.description else ""
                report_lines.append(
                    f"| {diff.source_id} | {diff.name} | {diff.credential_type_name} | "
                    f"{org_name} | {description} |"
                )

            report_lines.extend([
                "",
                "### Details",
                "",
            ])

            for i, diff in enumerate(result.missing_in_target, 1):
                org_info = f"Organization: {diff.organization_name} (ID: {diff.organization})" if diff.organization else "Organization: None (Global)"

                report_lines.extend([
                    f"#### {i}. {diff.name}",
                    f"- **Source ID:** {diff.source_id}",
                    f"- **Type:** {diff.credential_type_name} (ID: {diff.credential_type})",
                    f"- **{org_info}**",
                    f"- **Description:** {diff.description}",
                    f"- **Inputs:** `{list(diff.inputs.keys())}` (values are encrypted)",
                    "",
                ])
        else:
            report_lines.extend([
                "## All Credentials Present",
                "",
                "All source credentials already exist in the target instance.",
                "",
            ])

        report_lines.extend([
            "---",
            "",
            "## Next Steps",
            "",
        ])

        if result.missing_in_target:
            report_lines.extend([
                "1. Review missing credentials above",
                "2. Run migration to create missing credentials",
                "3. Note: Secret values will need manual entry (API returns `$encrypted$`)",
                "",
            ])
        else:
            report_lines.extend([
                "No action required - all credentials are present in target.",
                "",
            ])

        return "\n".join(report_lines)
