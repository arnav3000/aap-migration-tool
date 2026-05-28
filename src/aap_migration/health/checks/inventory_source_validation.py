"""Inventory source validation health check."""

import asyncio
from typing import Any

import structlog

from aap_migration.health.base_check import BaseHealthCheck
from aap_migration.health.models import CheckStatus, HealthCheckResult, Severity

logger = structlog.get_logger()


class InventorySourceValidationCheck(BaseHealthCheck):
    """Validate inventory source configuration issues that cause migration failures.

    Based on real customer data analysis:
    - 1 failure: Smart/constructed inventory with source
    - 3 failures: SCM sources missing source_project
    - Total: 4 preventable failures (3% of all inventory sources)
    """

    @property
    def check_name(self) -> str:
        """Name of the health check."""
        return "inventory_source_validation"

    @property
    def description(self) -> str:
        """Description of what this check validates."""
        return "Validate inventory source configurations (smart inventories, SCM sources)"

    async def run(self) -> HealthCheckResult:
        """Execute the inventory source validation check.

        Returns:
            HealthCheckResult with findings
        """
        logger.info(
            "health_check_starting",
            check=self.check_name,
            description=self.description,
        )

        # Fetch all inventory sources
        try:
            inventory_sources = await self._fetch_resources("inventory_sources")
        except Exception as e:
            logger.error(
                "failed_to_fetch_inventory_sources",
                error=str(e),
            )
            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.CRITICAL,
                status=CheckStatus.ERROR,
                message=f"Failed to fetch inventory sources: {str(e)}",
                details={},
                recommendation="Check AAP API connectivity and permissions",
                affected_resources=[],
                count=0,
            )

        logger.info(
            "inventory_sources_fetched",
            count=len(inventory_sources),
        )

        # Need to fetch inventories to check their kind
        try:
            inventories = await self._fetch_resources("inventories")
        except Exception as e:
            logger.error(
                "failed_to_fetch_inventories",
                error=str(e),
            )
            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.CRITICAL,
                status=CheckStatus.ERROR,
                message=f"Failed to fetch inventories: {str(e)}",
                details={},
                recommendation="Check AAP API connectivity and permissions",
                affected_resources=[],
                count=0,
            )

        # Build inventory lookup by ID
        inventory_lookup = {inv["id"]: inv for inv in inventories}

        # Track issues
        smart_inventory_sources = []
        scm_missing_project = []

        # Validate each inventory source
        for source in inventory_sources:
            inventory_id = source.get("inventory")
            if not inventory_id:
                continue

            inventory = inventory_lookup.get(inventory_id)
            if not inventory:
                logger.warning(
                    "inventory_source_orphaned_inventory",
                    source_id=source.get("id"),
                    inventory_id=inventory_id,
                )
                continue

            # Check 1: Smart or constructed inventory with source
            inventory_kind = inventory.get("kind", "")
            if inventory_kind in ["smart", "constructed"]:
                smart_inventory_sources.append(
                    {
                        "type": "inventory_sources",
                        "id": source.get("id"),
                        "name": source.get("name"),
                        "url": source.get("url"),
                        "issue": f"Inventory source for {inventory_kind} inventory",
                        "inventory_id": inventory_id,
                        "inventory_name": inventory.get("name"),
                        "inventory_kind": inventory_kind,
                    }
                )
                logger.debug(
                    "inventory_source_smart_inventory",
                    source_id=source.get("id"),
                    source_name=source.get("name"),
                    inventory_kind=inventory_kind,
                )

            # Check 2: SCM source missing source_project
            source_type = source.get("source", "")
            if source_type == "scm":
                if not source.get("source_project"):
                    scm_missing_project.append(
                        {
                            "type": "inventory_sources",
                            "id": source.get("id"),
                            "name": source.get("name"),
                            "url": source.get("url"),
                            "issue": "SCM source missing source_project",
                            "source": source_type,
                            "source_project": None,
                        }
                    )
                    logger.debug(
                        "inventory_source_scm_missing_project",
                        source_id=source.get("id"),
                        source_name=source.get("name"),
                    )

        # Calculate totals
        total_issues = len(smart_inventory_sources) + len(scm_missing_project)

        # Build result
        if total_issues > 0:
            details = {}
            if smart_inventory_sources:
                details["smart_inventory_sources"] = {
                    "count": len(smart_inventory_sources),
                    "description": "Inventory sources attached to smart or constructed inventories",
                    "error_on_migration": "Cannot create Inventory Source for Smart or Constructed Inventories",
                }
            if scm_missing_project:
                details["scm_missing_project"] = {
                    "count": len(scm_missing_project),
                    "description": "SCM inventory sources without source_project",
                    "error_on_migration": "Project required for scm type sources.",
                }

            message = f"Found {total_issues} inventory source configuration issues across {len(inventory_sources)} sources"

            recommendation = self._build_recommendation(
                len(smart_inventory_sources),
                len(scm_missing_project),
            )

            affected_resources = smart_inventory_sources + scm_missing_project

            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.CRITICAL,
                status=CheckStatus.FAIL,
                message=message,
                details=details,
                recommendation=recommendation,
                affected_resources=affected_resources,
                count=total_issues,
            )
        else:
            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.INFO,
                status=CheckStatus.PASS,
                message=f"All {len(inventory_sources)} inventory sources have valid configuration",
                details={},
                recommendation="",
                affected_resources=[],
                count=0,
            )

    def _build_recommendation(
        self,
        smart_inventory_count: int,
        scm_missing_project_count: int,
    ) -> str:
        """Build remediation recommendation based on issues found.

        Args:
            smart_inventory_count: Number of sources on smart/constructed inventories
            scm_missing_project_count: Number of SCM sources missing project

        Returns:
            Formatted recommendation text
        """
        recommendations = []

        if smart_inventory_count > 0:
            recommendations.append(
                f"\n**Smart/Constructed Inventory Sources ({smart_inventory_count} sources):**\n"
                "1. In source AAP UI, navigate to each affected inventory\n"
                "2. Delete the inventory source (smart/constructed inventories don't need sources)\n"
                "\n"
                "**Why this fails:** AAP 2.6 correctly prevents inventory sources on smart and "
                "constructed inventories. Smart inventories use host filters to dynamically select "
                "hosts from other inventories. Constructed inventories use input inventories. "
                "Neither should have their own inventory sources. This appears to be a data "
                "integrity issue in source AAP."
            )

        if scm_missing_project_count > 0:
            recommendations.append(
                f"\n**SCM Sources Missing Project ({scm_missing_project_count} sources):**\n"
                "1. In source AAP UI, navigate to each affected inventory source\n"
                "2. In the Source field, verify it's set to 'Sourced from a Project'\n"
                "3. Assign a project in the Project field\n"
                "4. Save the inventory source\n"
                "\n"
                "**Why this fails:** AAP 2.6 requires SCM-type inventory sources to have a "
                "project assigned. The project contains the inventory file that will be synced."
            )

        if recommendations:
            header = (
                "Fix the following inventory source issues in source AAP before migration:\n"
            )
            return header + "\n".join(recommendations)
        else:
            return ""
