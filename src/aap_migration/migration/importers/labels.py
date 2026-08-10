from collections.abc import Callable
from typing import Any

from aap_migration.migration.importers.base import ResourceImporter


class LabelImporter(ResourceImporter):
    """Importer for label resources."""

    DEPENDENCIES: dict[str, str] = {
        "organization": "organizations",
    }

    async def import_labels(
        self,
        labels: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple labels concurrently with live progress updates.

        Args:
            labels: List of label data
            progress_callback: Optional callback for progress updates.
                Called after each label with (success_count, failed_count).

        Returns:
            List of created label data
        """
        return await self._import_parallel("labels", labels, progress_callback)
