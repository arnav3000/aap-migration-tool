"""Domain resource importers (split from migration.importer; re-exported)."""

from collections.abc import Callable
from typing import Any, cast

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

        # Get instance hostname mappings from config/mappings.yaml
        instance_mappings = self.resource_mappings.get("instances") or {}

        # Fetch all target instances once
        target_instances = await self.client.list_resources("instances")
        target_by_hostname = {inst["hostname"]: inst for inst in target_instances}

        logger.info(
            "instance_mapping_started",
            source_count=len(instances),
            target_count=len(target_instances),
            configured_mappings=len(instance_mappings),
        )

        for instance in instances:
            source_id = instance.get("_source_id") or instance.get("id")
            source_hostname = instance.get("hostname", "unknown")

            # Check if already mapped
            if self.state.is_migrated("instances", cast(int, source_id)):
                skipped_count += 1
                if progress_callback:
                    progress_callback(success_count, failed_count, skipped_count)
                continue

            # Mark in progress
            self.state.mark_in_progress(
                resource_type="instances",
                source_id=cast(int, source_id),
                source_name=source_hostname,
                phase="import",
            )

            # Resolve target hostname (from mapping or exact match)
            target_hostname = instance_mappings.get(source_hostname, source_hostname)
            target_instance = target_by_hostname.get(target_hostname)

            if target_instance:
                # Found match - save ID mapping
                self.state.mark_completed(
                    resource_type="instances",
                    source_id=cast(int, source_id),
                    target_id=target_instance["id"],
                    target_name=target_instance["hostname"],
                )
                results.append(target_instance)
                success_count += 1
                self.stats["imported_count"] += 1
                logger.info(
                    "instance_mapped",
                    source_id=source_id,
                    target_id=target_instance["id"],
                    source_hostname=source_hostname,
                    target_hostname=target_hostname,
                )
            else:
                # No match found - log warning with hint
                error_msg = (
                    f"No target instance for '{source_hostname}'. "
                    f"Add mapping to config/mappings.yaml"
                )
                self.state.mark_failed(
                    resource_type="instances",
                    source_id=cast(int, source_id),
                    error_message=error_msg,
                )
                failed_count += 1
                self.stats["error_count"] += 1
                logger.warning(
                    "instance_not_found_on_target",
                    source_id=source_id,
                    source_hostname=source_hostname,
                    target_hostname=target_hostname,
                    hint="Add to config/mappings.yaml: instances: { source: target }",
                )

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

    DEPENDENCIES = {
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
