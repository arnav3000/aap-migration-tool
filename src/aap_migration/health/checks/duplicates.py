"""Duplicate resource health check."""

from collections import defaultdict
from typing import Any

import structlog

from aap_migration.health.base_check import BaseHealthCheck
from aap_migration.health.models import CheckStatus, HealthCheckResult, Severity

logger = structlog.get_logger()


class DuplicateCheck(BaseHealthCheck):
    """Check for duplicate resources (same name in same scope)."""

    # Organization-scoped resources (unique within organization)
    ORG_SCOPED_RESOURCES = {
        "job_templates": "organization",
        "workflow_job_templates": "organization",
        "projects": "organization",
        "inventories": "organization",
        "credentials": ("organization", "credential_type"),  # Composite key
        "teams": "organization",
        "notification_templates": "organization",
    }

    # Parent-scoped resources (unique within parent)
    PARENT_SCOPED_RESOURCES = {
        "inventory_sources": "inventory",
        "hosts": "inventory",
        "groups": "inventory",
    }

    @property
    def check_name(self) -> str:
        """Name of the health check."""
        return "duplicates"

    @property
    def description(self) -> str:
        """Description of what this check validates."""
        return "Check for duplicate resources with same name in same scope"

    async def run(self) -> HealthCheckResult:
        """Execute the duplicate check.

        Returns:
            HealthCheckResult with findings
        """
        logger.info(
            "health_check_starting",
            check=self.check_name,
            description=self.description,
        )

        all_duplicates = []
        total_duplicates = 0

        # Check organization-scoped resources
        for resource_type, scope_field in self.ORG_SCOPED_RESOURCES.items():
            duplicates = await self._check_org_scoped_duplicates(
                resource_type, scope_field
            )
            if duplicates:
                all_duplicates.extend(duplicates)
                total_duplicates += len(duplicates)

        # Check parent-scoped resources
        for resource_type, parent_field in self.PARENT_SCOPED_RESOURCES.items():
            duplicates = await self._check_parent_scoped_duplicates(
                resource_type, parent_field
            )
            if duplicates:
                all_duplicates.extend(duplicates)
                total_duplicates += len(duplicates)

        # Build result
        if total_duplicates > 0:
            message = f"Found {total_duplicates} duplicate resource groups"
            recommendation = (
                "Review and delete duplicate resources before migration:\n"
                "1. Export duplicate list using: aap-bridge health-check --show-duplicates --csv duplicates.csv\n"
                "2. Review duplicates in AAP source UI\n"
                "3. Delete unwanted duplicates (keep one, delete others)\n"
                "4. Re-run health check to verify\n\n"
                "Note: Migration tool will keep first occurrence and skip others, "
                "but it's better to clean up beforehand to ensure the correct resource is kept."
            )

            # Group duplicates by resource type for details
            duplicates_by_type = defaultdict(list)
            for dup in all_duplicates:
                duplicates_by_type[dup["resource_type"]].append(dup)

            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.CRITICAL,
                status=CheckStatus.FAIL,
                message=message,
                details=dict(duplicates_by_type),
                recommendation=recommendation,
                affected_resources=all_duplicates[:50],  # Limit to first 50
                count=total_duplicates,
            )
        else:
            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.INFO,
                status=CheckStatus.PASS,
                message="No duplicate resources found",
                details={},
                recommendation="",
                affected_resources=[],
                count=0,
            )

    async def _check_org_scoped_duplicates(
        self,
        resource_type: str,
        scope_field: str | tuple[str, ...],
    ) -> list[dict[str, Any]]:
        """Check for duplicates in organization-scoped resources.

        Args:
            resource_type: Resource type to check
            scope_field: Organization field name or composite key tuple

        Returns:
            List of duplicate groups
        """
        try:
            resources = await self._fetch_resources(resource_type)

            # Group by composite key (name + scope)
            groups = defaultdict(list)

            for resource in resources:
                name = resource.get("name")
                if not name:
                    continue

                # Build scope key
                if isinstance(scope_field, tuple):
                    # Composite key (e.g., credentials: name + org + credential_type)
                    scope_values = tuple(resource.get(f) for f in scope_field)
                    key = (name,) + scope_values
                else:
                    # Single key (e.g., job_templates: name + org)
                    scope_value = resource.get(scope_field)
                    key = (name, scope_value)

                groups[key].append(resource)

            # Find groups with duplicates
            duplicates = []
            for key, group in groups.items():
                if len(group) > 1:
                    # Found duplicates
                    name = key[0]
                    scope_info = key[1:] if len(key) > 1 else ("unknown",)

                    duplicates.append(
                        {
                            "resource_type": resource_type,
                            "name": name,
                            "scope": scope_info,
                            "count": len(group),
                            "ids": [r.get("id") for r in group],
                            "instances": [
                                {
                                    "id": r.get("id"),
                                    "name": r.get("name"),
                                    "url": r.get("url"),
                                }
                                for r in group
                            ],
                        }
                    )

                    logger.info(
                        "duplicates_found",
                        resource_type=resource_type,
                        name=name,
                        count=len(group),
                        ids=[r.get("id") for r in group],
                    )

            return duplicates

        except Exception as e:
            logger.warning(
                "duplicate_check_failed",
                resource_type=resource_type,
                error=str(e),
            )
            return []

    async def _check_parent_scoped_duplicates(
        self,
        resource_type: str,
        parent_field: str,
    ) -> list[dict[str, Any]]:
        """Check for duplicates in parent-scoped resources.

        Args:
            resource_type: Resource type to check
            parent_field: Parent field name (e.g., "inventory")

        Returns:
            List of duplicate groups
        """
        try:
            resources = await self._fetch_resources(resource_type)

            # Group by (name, parent)
            groups = defaultdict(list)

            for resource in resources:
                name = resource.get("name")
                parent_id = resource.get(parent_field)

                if not name:
                    continue

                key = (name, parent_id)
                groups[key].append(resource)

            # Find groups with duplicates
            duplicates = []
            for (name, parent_id), group in groups.items():
                if len(group) > 1:
                    duplicates.append(
                        {
                            "resource_type": resource_type,
                            "name": name,
                            "parent_field": parent_field,
                            "parent_id": parent_id,
                            "count": len(group),
                            "ids": [r.get("id") for r in group],
                            "instances": [
                                {
                                    "id": r.get("id"),
                                    "name": r.get("name"),
                                    "url": r.get("url"),
                                }
                                for r in group
                            ],
                        }
                    )

                    logger.info(
                        "duplicates_found",
                        resource_type=resource_type,
                        name=name,
                        parent_id=parent_id,
                        count=len(group),
                    )

            return duplicates

        except Exception as e:
            logger.warning(
                "duplicate_check_failed",
                resource_type=resource_type,
                error=str(e),
            )
            return []
