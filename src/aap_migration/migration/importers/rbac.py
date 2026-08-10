from typing import Any

from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class RBACImporter(ResourceImporter):
    """Importer for RBAC (Role-Based Access Control) role assignments.

    Handles granting roles to users and teams on various resource types.
    Does not have traditional dependencies as it operates on already-imported resources.
    """

    async def import_role_assignments(
        self, assignments: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Import RBAC role assignments.

        Each assignment grants a specific role to a user or team on a resource.

        Args:
            assignments: List of role assignment data with structure:
                {
                    "resource_type": "organizations",
                    "resource_id": 123,  # Source resource ID
                    "role": "admin",
                    "user": 456,  # Source user ID (mutually exclusive with team)
                    "team": 789,  # Source team ID (mutually exclusive with user)
                }

        Returns:
            List of successfully granted role assignment data
        """
        results = []

        for assignment in assignments:
            try:
                resource_type = assignment["resource_type"]
                source_resource_id = assignment["resource_id"]
                role_name = assignment["role"]
                source_user_id = assignment.get("user")
                source_team_id = assignment.get("team")

                # Resolve resource ID
                target_resource_id = self.state.get_mapped_id(resource_type, source_resource_id)
                if not target_resource_id:
                    logger.warning(
                        "rbac_resource_not_found",
                        resource_type=resource_type,
                        source_id=source_resource_id,
                    )
                    continue

                # Resolve user or team ID (prefer user if both are present)
                if source_user_id:
                    target_principal_id = self.state.get_mapped_id("users", source_user_id)
                    if not target_principal_id:
                        logger.warning(
                            "rbac_user_not_found",
                            source_user_id=source_user_id,
                        )
                        continue
                    principal_key = "user"
                    principal_id = target_principal_id
                elif source_team_id:
                    target_principal_id = self.state.get_mapped_id("teams", source_team_id)
                    if not target_principal_id:
                        logger.warning(
                            "rbac_team_not_found",
                            source_team_id=source_team_id,
                        )
                        continue
                    principal_key = "team"
                    principal_id = target_principal_id
                else:
                    logger.warning(
                        "rbac_no_principal",
                        resource_type=resource_type,
                        source_resource_id=source_resource_id,
                        assignment=assignment,
                    )
                    continue

                # Grant role via AAP API
                # Endpoint format: {resource_type}/{id}/roles/{role_name}/{principal_type}s/
                principal_type_plural = f"{principal_key}s"
                endpoint = f"{resource_type}/{target_resource_id}/roles/{role_name}/{principal_type_plural}/"
                data = {"id": principal_id}

                logger.info(
                    "granting_role",
                    resource_type=resource_type,
                    source_resource_id=source_resource_id,
                    target_resource_id=target_resource_id,
                    role=role_name,
                    principal_type=principal_key,
                    source_principal_id=source_user_id or source_team_id,
                    target_principal_id=principal_id,
                )

                result = await self.client.post(
                    endpoint=endpoint,
                    data=data,
                )

                results.append(result)
                self.stats["imported_count"] += 1

            except Exception as e:
                logger.error(
                    "rbac_import_error",
                    resource_type=resource_type,
                    source_resource_id=source_resource_id,
                    role=role_name,
                    assignment=assignment,
                    error=str(e),
                )
                self.stats["error_count"] += 1

                # Track error for reporting
                self.import_errors.append(
                    {
                        "resource_type": "rbac_assignments",
                        "source_id": source_resource_id,
                        "name": f"{resource_type}/{role_name}",
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "details": assignment,
                    }
                )

                continue

        return results
