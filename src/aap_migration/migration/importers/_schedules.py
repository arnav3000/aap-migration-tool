"""Domain resource importers (split from migration.importer; re-exported)."""

from collections.abc import Callable
from typing import Any

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

    DEPENDENCIES = {
        "inventory": "inventories",
        "execution_environment": "execution_environments",
    }  # unified_job_template handled manually in _resolve_dependencies

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
                    # Log warning but keep original ID (will likely fail)
                    logger.warning(
                        "schedule_dependency_unresolved",
                        source_id=data.get("id"),
                        ujt_type=ujt_type,
                        ujt_id=ujt_id,
                        message=f"Could not resolve {ujt_type} ID {ujt_id}",
                    )
            elif ujt_id:
                # Missing type information
                logger.warning(
                    "schedule_missing_ujt_type",
                    source_id=data.get("id"),
                    ujt_id=ujt_id,
                    message="Missing _ujt_resource_type for schedule",
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

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Override to route inventory_source schedules to nested endpoint.

        # NOTE: Inventory source schedules must use nested endpoint - flat /schedules/
        # endpoint rejects inventory_source UJTs
        #
        # The flat POST /api/v2/schedules/ endpoint rejects inventory_source
        # unified_job_templates, causing 100% failure for inventory_source schedules.
        # Instead, we route them to POST /inventory_sources/{target_id}/schedules/
        # which works correctly (same pattern as InventorySourceImporter inline code).
        #
        # All other schedule types (job_template, workflow, project) continue to
        # use the flat endpoint via the base class import_resource.
        """
        # Capture _ujt_resource_type before _resolve_dependencies removes it
        ujt_resource_type = data.get("_ujt_resource_type")

        # Non-inventory_source schedules: use the standard flat endpoint (base class)
        if ujt_resource_type != "inventory_sources":
            return await super().import_resource(
                resource_type, source_id, data, resolve_dependencies
            )

        # --- Inventory source schedule: use nested endpoint ---

        # Check if already imported
        if self.state.is_migrated(resource_type, source_id):
            logger.debug(
                "resource_already_imported",
                resource_type=resource_type,
                source_id=source_id,
            )
            self.stats["skipped_count"] += 1
            return None

        # Mark as in progress
        self.state.mark_in_progress(
            resource_type=resource_type,
            source_id=source_id,
            source_name=data.get("name", "unknown"),
            phase="import",
        )

        try:
            # Resolve dependencies (this resolves unified_job_template to target ID,
            # removes _ujt_resource_type, and disables schedule for safety)
            resolved = await self._resolve_dependencies(resource_type, data)

            # Get the resolved target inventory source ID
            target_id = resolved.get("unified_job_template")
            if not target_id:
                error_msg = (
                    f"Cannot import inventory_source schedule: "
                    f"unified_job_template not resolved for source schedule "
                    f"'{data.get('name', 'unknown')}' (source ID {source_id})"
                )
                logger.error(
                    "inventory_source_schedule_ujt_unresolved",
                    source_id=source_id,
                    schedule_name=data.get("name"),
                    error=error_msg,
                )
                self.stats["error_count"] += 1
                self.state.mark_failed(
                    resource_type=resource_type,
                    source_id=source_id,
                    error_message=error_msg,
                )
                return None

            # Remove fields not needed for nested endpoint POST
            # (same pattern as InventorySourceImporter lines 2052-2056)
            schedule_to_import = {
                k: v
                for k, v in resolved.items()
                if k
                not in [
                    "id",
                    "type",
                    "url",
                    "related",
                    "summary_fields",
                    "created",
                    "modified",
                    "last_run",
                    "next_run",
                    "status",
                    "unified_job_template",
                ]
            }

            # Remove None values
            schedule_to_import = {k: v for k, v in schedule_to_import.items() if v is not None}

            # POST to nested endpoint
            result = await self.client.post(
                f"inventory_sources/{target_id}/schedules/",
                json_data=schedule_to_import,
            )

            # Log success (same pattern as InventorySourceImporter lines 2067-2075)
            logger.info(
                "inventory_source_schedule_imported",
                inventory_source_id=target_id,
                schedule_name=data.get("name", "unknown"),
                schedule_id=result.get("id"),
                source_id=source_id,
                original_enabled=data.get("enabled", True),
                imported_as_disabled=True,
            )

            # Mark as completed and track in database
            self.state.mark_completed(
                resource_type=resource_type,
                source_id=source_id,
                target_id=result["id"],
                target_name=result.get("name", data.get("name", "unknown")),
                source_name=data.get("name", "unknown"),
            )

            self.stats["imported_count"] += 1
            return result

        except Exception as e:
            logger.error(
                "inventory_source_schedule_import_failed",
                source_id=source_id,
                schedule_name=data.get("name", "unknown"),
                error=str(e),
            )

            self.stats["error_count"] += 1
            self.state.mark_failed(
                resource_type=resource_type,
                source_id=source_id,
                error_message=f"{type(e).__name__}: {str(e)}",
            )

            self.import_errors.append(
                {
                    "resource_type": resource_type,
                    "source_id": source_id,
                    "name": data.get("name", "unknown"),
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
            )

            return None

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
