from collections.abc import Callable
from typing import Any

from aap_migration.client.exceptions import DependencyError
from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class ScheduleImporter(ResourceImporter):
    """Importer for schedule resources.

    Schedules depend on unified_job_template which can reference:
    - job_templates
    - workflow_job_templates
    - inventory_sources
    """

    DEPENDENCIES: dict[str, str] = {}  # Handled manually in _resolve_dependencies

    async def _resolve_dependencies(
        self, resource_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve dependencies with polymorphic unified_job_template handling."""
        # Call parent to handle any standard dependencies
        resolved = await super()._resolve_dependencies(resource_type, data)

        # Handle polymorphic unified_job_template
        if "unified_job_template" in data:
            ujt_id = data["unified_job_template"]
            # _ujt_resource_type is added by ScheduleTransformer
            ujt_type = data.get("_ujt_resource_type")

            # Always remove the internal metadata field from the payload
            resolved.pop("_ujt_resource_type", None)

            if ujt_id and ujt_type:
                target_id = self.state.get_mapped_id(ujt_type, ujt_id)
                if target_id:
                    resolved["unified_job_template"] = target_id
                    logger.debug(
                        "schedule_dependency_resolved",
                        source_id=data.get("id"),
                        ujt_type=ujt_type,
                        ujt_id=ujt_id,
                        target_id=target_id,
                    )
                else:
                    resolved.pop("unified_job_template", None)
                    raise DependencyError(
                        f"Could not resolve {ujt_type} ID {ujt_id} for schedule "
                        f"(source_id={data.get('id')})"
                    )
            elif ujt_id:
                resolved.pop("unified_job_template", None)
                raise DependencyError(
                    f"Missing _ujt_resource_type for schedule unified_job_template "
                    f"{ujt_id} (source_id={data.get('id')})"
                )

        # SAFETY: Disable all migrated schedules by default
        # Prevents automatic execution in target AAP until manually verified and enabled
        # Schedules could trigger jobs, workflows, or project syncs immediately after import
        original_enabled = resolved.get("enabled", True)
        resolved["enabled"] = False

        if original_enabled:
            logger.info(
                "schedule_disabled_for_safety",
                source_id=data.get("_source_id"),
                source_name=data.get("name"),
                original_state="enabled",
                new_state="disabled",
                message="Schedule disabled on import for safety - enable manually in target AAP after verification",
            )

        return resolved

    async def import_schedules(
        self,
        schedules: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple schedules concurrently with live progress updates.

        Handles unified_job_template dependency which can point to various
        schedulable resources (job templates, workflows, inventory sources).
        Preserves RRULE format for recurrence patterns.

        Args:
            schedules: List of schedule data
            progress_callback: Optional callback for progress updates.
                Called after each schedule with (success_count, failed_count).

        Returns:
            List of created schedule data
        """
        return await self._import_parallel("schedules", schedules, progress_callback)
