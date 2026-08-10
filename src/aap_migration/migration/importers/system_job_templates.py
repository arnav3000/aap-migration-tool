from collections.abc import Callable
from typing import Any

from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class SystemJobTemplateImporter(ResourceImporter):
    """Importer for system job template resources.

    System job templates are built-in and read-only. We only map them.
    """

    DEPENDENCIES: dict[str, str] = {}

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Map system job template by name."""
        if self.state.is_migrated(resource_type, source_id):
            self.stats["skipped_count"] += 1
            return None

        name = data.get("name")
        if not name:
            return None

        self.state.mark_in_progress(resource_type, source_id, name, "import")

        try:
            # Lookup by name
            results = await self.client.get(
                "system_job_templates/",
                params={"name": name},
            )
            resources = results.get("results", [])

            if resources:
                target_id = resources[0]["id"]
                self.state.save_id_mapping(
                    resource_type=resource_type,
                    source_id=source_id,
                    target_id=target_id,
                    source_name=name,
                    target_name=name,
                )
                self.state.mark_completed(resource_type, source_id, target_id, name)
                self.stats["imported_count"] += 1
                logger.info(
                    "system_job_template_mapped",
                    source_id=source_id,
                    target_id=target_id,
                    name=name,
                )
                return {"id": target_id, "name": name}
            else:
                logger.warning(
                    "system_job_template_not_found_in_target",
                    name=name,
                    source_id=source_id,
                )
                self.state.mark_failed(resource_type, source_id, "Not found in target")
                self.stats["error_count"] += 1
                return None

        except Exception as e:
            logger.error(
                "system_job_template_import_failed",
                name=name,
                error=str(e),
            )
            self.state.mark_failed(resource_type, source_id, str(e))
            self.stats["error_count"] += 1
            return None

    async def import_system_job_templates(
        self,
        templates: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple system job templates (mapping only)."""
        # Extract schedules before import
        templates_with_schedules = []
        for template in templates:
            schedules = template.pop("schedules", None)
            if schedules:
                source_id = template.get("_source_id", template.get("id"))
                templates_with_schedules.append(
                    {
                        "source_template_id": source_id,
                        "schedules": schedules,
                    }
                )

        # Import (map) system job templates
        results = await self._import_parallel("system_job_templates", templates, progress_callback)

        # Import schedules for successfully mapped system job templates
        if templates_with_schedules:
            logger.info(
                "importing_system_job_template_schedules",
                total_templates_with_schedules=len(templates_with_schedules),
            )

            for schedule_data in templates_with_schedules:
                source_template_id = schedule_data["source_template_id"]
                schedules = schedule_data["schedules"]

                # Get the target system job template ID from the state mapping
                target_template_id = self.state.get_mapped_id(
                    "system_job_templates", source_template_id
                )
                if not target_template_id:
                    logger.warning(
                        "system_job_template_not_found_for_schedule",
                        source_template_id=source_template_id,
                    )
                    continue

                # Get system job template name for logging
                template_result = next(
                    (t for t in results if t.get("id") == target_template_id), None
                )
                template_name = (
                    template_result.get("name", "unknown") if template_result else "unknown"
                )

                for schedule in schedules:
                    schedule_name = schedule.get("name", "unknown")

                    # Remove read-only fields
                    schedule_to_import = {
                        k: v
                        for k, v in schedule.items()
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

                    try:
                        result = await self.client.post(
                            f"system_job_templates/{target_template_id}/schedules/",
                            json_data=schedule_to_import,
                        )
                        logger.info(
                            "system_job_template_schedule_imported",
                            template_id=target_template_id,
                            template_name=template_name,
                            schedule_name=schedule_name,
                            schedule_id=result.get("id"),
                        )
                    except Exception as e:
                        logger.error(
                            "system_job_template_schedule_import_failed",
                            template_id=target_template_id,
                            template_name=template_name,
                            schedule_name=schedule_name,
                            error=str(e),
                        )

        return results
