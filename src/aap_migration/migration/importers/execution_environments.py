from collections.abc import Callable
from typing import Any

from aap_migration.migration.importers.base import ResourceImporter


class ExecutionEnvironmentImporter(ResourceImporter):
    """Importer for execution environment resources.

    Execution Environments are container images that provide the Ansible
    runtime environment. They depend on:
    - organization (required)
    - credential (optional, for private registries)
    """

    DEPENDENCIES: dict[str, str] = {
        "organization": "organizations",
        "credential": "credentials",
    }

    async def import_execution_environments(
        self,
        ees: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple execution environments concurrently with live progress updates.

        Handles organization and optional credential dependency resolution.

        Args:
            ees: List of execution environment data
            progress_callback: Optional callback for progress updates.
                Called after each execution environment with (success_count, failed_count).

        Returns:
            List of created execution environment data
        """
        return await self._import_parallel("execution_environments", ees, progress_callback)
