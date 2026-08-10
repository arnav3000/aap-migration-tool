from collections.abc import Callable
from typing import Any

from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class InstanceImporter(ResourceImporter):
    """Importer for instance (AAP controller node) resources.

    Instances are infrastructure nodes that cannot be created via API.
    Instead, we match source instances to existing target instances by hostname
    and create ID mappings for instance_group references.

    Uses config/mappings.yaml to map different hostnames between environments.
    """

    DEPENDENCIES: dict[str, str] = {}  # No dependencies - instances are foundational
    IDENTIFIER_FIELD = "hostname"  # Instances use 'hostname' instead of 'name'

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._target_instances_by_hostname: dict[str, dict[str, Any]] | None = None

    async def _get_target_instances_by_hostname(self) -> dict[str, dict[str, Any]]:
        if self._target_instances_by_hostname is None:
            target_instances = await self.client.list_resources("instances")
            self._target_instances_by_hostname = {
                inst["hostname"]: inst for inst in target_instances
            }
        return self._target_instances_by_hostname

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Map a single source instance to a target instance by hostname.

        Planner and single-resource paths call this instead of create_resource —
        instances cannot be created via the AAP API.
        """
        _ = resolve_dependencies
        source_hostname = data.get("hostname") or data.get("name") or "unknown"

        if self.state.is_migrated(resource_type, source_id):
            target_id = self.state.get_mapped_id(resource_type, source_id)
            self.stats["skipped_count"] += 1
            return {
                "id": target_id,
                "hostname": source_hostname,
                "_already_migrated": True,
                "_skip_reason": (
                    f"Instance already mapped (target id {target_id})"
                    if target_id is not None
                    else "Instance already marked migrated in state"
                ),
            }

        self.state.mark_in_progress(
            resource_type=resource_type,
            source_id=source_id,
            source_name=source_hostname,
            phase="import",
        )

        instance_mappings = self.resource_mappings.get("instances") or {}
        target_hostname = instance_mappings.get(source_hostname, source_hostname)
        target_by_hostname = await self._get_target_instances_by_hostname()
        target_instance = target_by_hostname.get(target_hostname)

        if not target_instance:
            error_msg = (
                f"No target instance for '{source_hostname}'. Add mapping to config/mappings.yaml"
            )
            self.state.mark_failed(
                resource_type=resource_type,
                source_id=source_id,
                error_message=error_msg,
            )
            self.stats["error_count"] += 1
            self.import_errors.append(
                {
                    "resource_type": resource_type,
                    "source_id": source_id,
                    "name": source_hostname,
                    "error": error_msg,
                    "error_type": "InstanceMappingError",
                }
            )
            logger.warning(
                "instance_not_found_on_target",
                source_id=source_id,
                source_hostname=source_hostname,
                target_hostname=target_hostname,
                hint="Add to config/mappings.yaml: instances: { source: target }",
            )
            return None

        target_id = int(target_instance["id"])
        self.state.save_id_mapping(
            resource_type=resource_type,
            source_id=source_id,
            target_id=target_id,
            source_name=source_hostname,
            target_name=target_instance.get("hostname", target_hostname),
        )
        self.state.mark_completed(
            resource_type=resource_type,
            source_id=source_id,
            target_id=target_id,
            target_name=target_instance.get("hostname", target_hostname),
        )
        self.stats["imported_count"] += 1
        logger.info(
            "instance_mapped",
            source_id=source_id,
            target_id=target_id,
            source_hostname=source_hostname,
            target_hostname=target_hostname,
        )
        return target_instance

    async def import_instances(
        self,
        instances: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Map source instances to existing target instances via configuration.

        Instances cannot be created via API - they're infrastructure nodes.
        This method finds matching instances on target and creates ID mappings.

        Uses mappings from config/mappings.yaml to resolve different hostnames.
        Falls back to exact hostname match if no explicit mapping exists.

        Args:
            instances: List of instance data from source
            progress_callback: Optional callback for progress updates.
                Called after each instance with (success_count, failed_count, skipped_count).

        Returns:
            List of matched target instance data
        """
        results = []
        success_count = 0
        failed_count = 0
        skipped_count = 0

        for instance in instances:
            raw_sid = instance.get("_source_id") or instance.get("id")
            if raw_sid is None:
                logger.warning(
                    "instance_missing_source_id",
                    hostname=instance.get("hostname"),
                    message="Skipping instance without source id",
                )
                failed_count += 1
                self.stats["error_count"] += 1
                if progress_callback:
                    progress_callback(success_count, failed_count, skipped_count)
                continue

            source_id = int(raw_sid)
            result = await self.import_resource("instances", source_id, instance)
            if result and result.get("_already_migrated"):
                skipped_count += 1
            elif result:
                results.append(result)
                success_count += 1
            else:
                failed_count += 1

            if progress_callback:
                progress_callback(success_count, failed_count, skipped_count)

        logger.info(
            "instance_mapping_completed",
            mapped=success_count,
            failed=failed_count,
            skipped=skipped_count,
        )

        return results


class InstanceGroupImporter(ResourceImporter):
    """Importer for instance group resources."""

    DEPENDENCIES: dict[str, str] = {
        "credential": "credentials",  # For container instance groups
    }

    async def import_instance_groups(
        self,
        instance_groups: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple instance groups concurrently with live progress updates.

        Args:
            instance_groups: List of instance group data
            progress_callback: Optional callback for progress updates.
                Called after each instance group with (success_count, failed_count).

        Returns:
            List of created instance group data
        """
        return await self._import_parallel("instance_groups", instance_groups, progress_callback)
