from collections.abc import Callable
from typing import Any

from aap_migration.migration.importers.base import ResourceImporter


class NotificationTemplateImporter(ResourceImporter):
    """Importer for notification template resources.

    Notification templates define how AAP sends notifications about
    job status (email, Slack, webhook, etc.).
    """

    DEPENDENCIES: dict[str, str] = {
        "organization": "organizations",
    }

    async def import_notification_templates(
        self,
        notifications: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple notification templates concurrently with live progress updates.

        Args:
            notifications: List of notification template data
            progress_callback: Optional callback for progress updates.
                Called after each notification with (success_count, failed_count).

        Returns:
            List of created notification template data
        """
        return await self._import_parallel(
            "notification_templates", notifications, progress_callback
        )
