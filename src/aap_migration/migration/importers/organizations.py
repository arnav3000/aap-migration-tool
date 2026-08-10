from collections.abc import Callable
from typing import Any

from aap_migration.migration.importers.base import ResourceImporter


class OrganizationImporter(ResourceImporter):
    """Importer for organization resources."""

    DEPENDENCIES: dict[str, str] = {
        "default_environment": "execution_environments",
    }

    async def import_organizations(
        self,
        organizations: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple organizations concurrently with live progress updates.

        Args:
            organizations: List of organization data
            progress_callback: Optional callback for progress updates.
                Called after each organization with (success_count, failed_count).

        Returns:
            List of created organization data
        """
        return await self._import_parallel("organizations", organizations, progress_callback)
