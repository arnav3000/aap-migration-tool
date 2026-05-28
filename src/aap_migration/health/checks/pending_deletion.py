"""Pending deletion health check."""

import structlog

from aap_migration.health.base_check import BaseHealthCheck
from aap_migration.health.models import CheckStatus, HealthCheckResult, Severity

logger = structlog.get_logger()


class PendingDeletionCheck(BaseHealthCheck):
    """Check for resources pending deletion."""

    # Resource types that support pending_deletion field
    RESOURCE_TYPES = [
        "job_templates",
        "workflow_job_templates",
        "projects",
        "inventories",
        "credentials",
        "organizations",
        "teams",
        "notification_templates",
        "inventory_sources",
    ]

    @property
    def check_name(self) -> str:
        """Name of the health check."""
        return "pending_deletion"

    @property
    def description(self) -> str:
        """Description of what this check validates."""
        return "Check for resources marked for deletion but not yet purged"

    async def run(self) -> HealthCheckResult:
        """Execute the pending deletion check.

        Returns:
            HealthCheckResult with findings
        """
        logger.info(
            "health_check_starting",
            check=self.check_name,
            description=self.description,
        )

        pending_by_type = {}
        total_pending = 0
        affected_resources = []

        # Check each resource type for pending deletion
        for resource_type in self.RESOURCE_TYPES:
            try:
                # Query for resources with pending_deletion=true
                resources = await self._fetch_resources(
                    resource_type,
                    params={"pending_deletion": "true"},
                )

                count = len(resources)
                if count > 0:
                    pending_by_type[resource_type] = count
                    total_pending += count

                    # Collect sample of affected resources (first 10)
                    for resource in resources[:10]:
                        affected_resources.append(
                            {
                                "type": resource_type,
                                "id": resource.get("id"),
                                "name": resource.get("name"),
                                "url": resource.get("url"),
                            }
                        )

                    logger.info(
                        "pending_deletion_found",
                        resource_type=resource_type,
                        count=count,
                    )

            except Exception as e:
                logger.warning(
                    "pending_deletion_check_failed",
                    resource_type=resource_type,
                    error=str(e),
                )

        # Build result
        if total_pending > 0:
            message = f"Found {total_pending} resources pending deletion across {len(pending_by_type)} resource types"
            recommendation = (
                "Clean up pending deletion objects before migration:\n"
                "1. Review list of pending deletion resources\n"
                "2. In AAP source UI, permanently delete unwanted resources\n"
                "3. Re-run health check to verify cleanup\n\n"
                "Pending deletion objects are marked for deletion but not yet purged. "
                "Migrating them wastes time and storage, and they may have broken references."
            )

            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.CRITICAL,
                status=CheckStatus.FAIL,
                message=message,
                details=pending_by_type,
                recommendation=recommendation,
                affected_resources=affected_resources,
                count=total_pending,
            )
        else:
            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.INFO,
                status=CheckStatus.PASS,
                message="No resources pending deletion found",
                details={},
                recommendation="",
                affected_resources=[],
                count=0,
            )
