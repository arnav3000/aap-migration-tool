"""Orphaned reference health check."""

from typing import Any

import structlog

from aap_migration.health.base_check import BaseHealthCheck
from aap_migration.health.models import CheckStatus, HealthCheckResult, Severity

logger = structlog.get_logger()


class OrphanedReferenceCheck(BaseHealthCheck):
    """Check for resources with broken foreign key references."""

    # Define resource dependencies to check
    # Format: {resource_type: [(field_name, referenced_resource_type), ...]}
    DEPENDENCIES = {
        "job_templates": [
            ("project", "projects"),
            ("inventory", "inventories"),
            ("playbook", None),  # Special case - checked against project
        ],
        "workflow_job_templates": [
            ("organization", "organizations"),
        ],
        "inventory_sources": [
            ("inventory", "inventories"),
            ("source_project", "projects"),
            ("credential", "credentials"),
        ],
        "schedules": [
            ("unified_job_template", None),  # Polymorphic - checked specially
        ],
    }

    @property
    def check_name(self) -> str:
        """Name of the health check."""
        return "orphaned_references"

    @property
    def description(self) -> str:
        """Description of what this check validates."""
        return "Check for resources with broken foreign key references"

    async def run(self) -> HealthCheckResult:
        """Execute the orphaned reference check.

        Returns:
            HealthCheckResult with findings
        """
        logger.info(
            "health_check_starting",
            check=self.check_name,
            description=self.description,
        )

        all_orphans = []
        total_orphans = 0

        # Check each resource type
        for resource_type, dependencies in self.DEPENDENCIES.items():
            orphans = await self._check_resource_references(resource_type, dependencies)
            if orphans:
                all_orphans.extend(orphans)
                total_orphans += len(orphans)

        # Build result
        if total_orphans > 0:
            message = f"Found {total_orphans} resources with orphaned references"
            recommendation = (
                "Fix broken references before migration:\n"
                "1. Export orphan list using: aap-bridge health-check --show-orphans --csv orphans.csv\n"
                "2. Review orphaned resources in AAP source UI\n"
                "3. Either:\n"
                "   a) Fix broken references (update to valid resources), OR\n"
                "   b) Accept that these resources won't migrate\n"
                "4. Re-run health check to verify\n\n"
                "Resources with orphaned references will be skipped during migration."
            )

            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.CRITICAL,
                status=CheckStatus.FAIL,
                message=message,
                details={"orphans_by_type": self._group_by_type(all_orphans)},
                recommendation=recommendation,
                affected_resources=all_orphans[:50],  # Limit to first 50
                count=total_orphans,
            )
        else:
            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.INFO,
                status=CheckStatus.PASS,
                message="No orphaned references found",
                details={},
                recommendation="",
                affected_resources=[],
                count=0,
            )

    async def _check_resource_references(
        self,
        resource_type: str,
        dependencies: list[tuple[str, str | None]],
    ) -> list[dict[str, Any]]:
        """Check references for a resource type.

        Args:
            resource_type: Resource type to check
            dependencies: List of (field_name, referenced_type) tuples

        Returns:
            List of resources with orphaned references
        """
        try:
            resources = await self._fetch_resources(resource_type)
            orphans = []

            # Build index of valid IDs for each referenced type
            valid_ids_cache = {}

            for resource in resources:
                resource_id = resource.get("id")
                resource_name = resource.get("name", "Unknown")
                missing_refs = []

                for field_name, referenced_type in dependencies:
                    field_value = resource.get(field_name)

                    # Skip if field is not set (null/None is valid)
                    if field_value is None:
                        continue

                    # Special handling for polymorphic fields
                    if referenced_type is None:
                        if field_name == "unified_job_template":
                            # Check unified_job_template polymorphically
                            is_orphaned = await self._check_polymorphic_reference(
                                resource, field_value
                            )
                            if is_orphaned:
                                missing_refs.append(
                                    {
                                        "field": field_name,
                                        "referenced_id": field_value,
                                        "referenced_type": "unified_job_template (polymorphic)",
                                    }
                                )
                        continue

                    # Check if referenced resource exists
                    if referenced_type not in valid_ids_cache:
                        # Fetch all IDs for this resource type
                        try:
                            ref_resources = await self._fetch_resources(referenced_type)
                            valid_ids_cache[referenced_type] = {
                                r.get("id") for r in ref_resources
                            }
                        except Exception as e:
                            logger.warning(
                                "failed_to_fetch_reference_resources",
                                resource_type=referenced_type,
                                error=str(e),
                            )
                            valid_ids_cache[referenced_type] = set()

                    # Check if reference is valid
                    if field_value not in valid_ids_cache[referenced_type]:
                        missing_refs.append(
                            {
                                "field": field_name,
                                "referenced_id": field_value,
                                "referenced_type": referenced_type,
                            }
                        )

                # If resource has missing references, add to orphans
                if missing_refs:
                    orphans.append(
                        {
                            "resource_type": resource_type,
                            "id": resource_id,
                            "name": resource_name,
                            "url": resource.get("url"),
                            "missing_references": missing_refs,
                        }
                    )

                    logger.info(
                        "orphaned_references_found",
                        resource_type=resource_type,
                        resource_id=resource_id,
                        resource_name=resource_name,
                        missing_count=len(missing_refs),
                    )

            return orphans

        except Exception as e:
            logger.warning(
                "orphaned_reference_check_failed",
                resource_type=resource_type,
                error=str(e),
            )
            return []

    async def _check_polymorphic_reference(
        self,
        resource: dict[str, Any],
        ujt_id: int,
    ) -> bool:
        """Check if polymorphic unified_job_template reference is orphaned.

        Args:
            resource: Resource with unified_job_template field
            ujt_id: Unified job template ID

        Returns:
            True if reference is orphaned, False otherwise
        """
        # Try to determine UJT type from summary_fields or related URL
        ujt_type = None

        if "summary_fields" in resource and "unified_job_template" in resource["summary_fields"]:
            ujt_summary = resource["summary_fields"]["unified_job_template"]
            ujt_type = ujt_summary.get("type")

        if not ujt_type and "related" in resource and "unified_job_template" in resource["related"]:
            ujt_url = resource["related"]["unified_job_template"]
            # Extract type from URL: /api/v2/job_templates/123/
            import re
            match = re.search(r"/api/v2/([^/]+)/\d+/", ujt_url)
            if match:
                ujt_type = match.group(1)

        if not ujt_type:
            # Can't determine type, assume it's not orphaned
            return False

        # Map API type to resource type
        type_map = {
            "job_template": "job_templates",
            "workflow_job_template": "workflow_job_templates",
            "project": "projects",
            "inventory_source": "inventory_sources",
            "system_job_template": "system_job_templates",
        }

        resource_type = type_map.get(ujt_type)
        if not resource_type:
            return False

        # Check if resource exists
        try:
            response = await self.client.get(f"{resource_type}/{ujt_id}/")
            return response is None or response.get("id") != ujt_id
        except Exception:
            # Resource doesn't exist or API error - consider orphaned
            return True

    def _group_by_type(self, orphans: list[dict[str, Any]]) -> dict[str, int]:
        """Group orphans by resource type.

        Args:
            orphans: List of orphaned resources

        Returns:
            Dict mapping resource_type to count
        """
        counts = {}
        for orphan in orphans:
            resource_type = orphan["resource_type"]
            counts[resource_type] = counts.get(resource_type, 0) + 1
        return counts
