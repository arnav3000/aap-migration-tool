from collections.abc import Callable
from typing import Any

from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class InventoryImporter(ResourceImporter):
    """Importer for inventory resources."""

    DEPENDENCIES: dict[str, str] = {
        "organization": "organizations",
    }

    async def import_inventories(
        self,
        inventories: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple inventories concurrently with live progress updates.

        Args:
            inventories: List of inventory data
            progress_callback: Optional callback for progress updates.
                Called after each inventory with (success_count, failed_count).

        Returns:
            List of created inventory data
        """
        return await self._import_parallel("inventories", inventories, progress_callback)


class InventoryGroupImporter(ResourceImporter):
    """Importer for inventory group resources.

    Handles nested hierarchies via topological sorting to ensure parents
    are imported before children.
    Uses optimized tier-based parallel import for performance.
    """

    DEPENDENCIES: dict[str, str] = {
        "inventory": "inventories",
        "parent": "inventory_groups",  # Link to parent group
    }

    # Override the API endpoint since "inventory_groups" maps to "groups/" in AAP API
    API_ENDPOINT = "groups"

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Import inventory group with correct API endpoint.

        Overrides parent to use 'groups' endpoint instead of 'inventory_groups'.
        """
        # Use "groups" for API call but keep "inventory_groups" for state tracking
        api_resource_type = (
            self.API_ENDPOINT if resource_type == "inventory_groups" else resource_type
        )

        # Track state with original resource_type
        if self.state.is_migrated(resource_type, source_id):
            self.stats["skipped_count"] += 1
            return None

        self.state.mark_in_progress(
            resource_type=resource_type,
            source_id=source_id,
            source_name=data.get("name", "unknown"),
            phase="import",
        )

        try:
            if resolve_dependencies:
                data = await self._resolve_dependencies(resource_type, data)

            # Use correct API endpoint
            result = await self.client.create_resource(
                resource_type=api_resource_type,
                data=data,
                check_exists=True,
            )

            target_id_raw = result.get("id")
            if target_id_raw is None:
                raise TypeError("create_resource returned no id for inventory group")
            target_id = int(target_id_raw)
            self.state.mark_completed(
                resource_type=resource_type,
                source_id=source_id,
                target_id=target_id,
            )

            self.stats["imported_count"] += 1
            logger.info(
                "resource_imported",
                resource_type=resource_type,
                source_id=source_id,
                target_id=target_id,
            )

            return result

        except Exception as e:
            self.state.mark_failed(
                resource_type=resource_type,
                source_id=source_id,
                error_message=str(e),
            )
            self.stats["error_count"] += 1

            # Track error for reporting
            self.import_errors.append(
                {
                    "resource_type": resource_type,
                    "source_id": source_id,
                    "name": data.get("name", "unknown"),
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
            )

            raise

    async def import_inventory_groups(
        self,
        groups: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple inventory groups with topological sorting and parallel execution.

        1. Sorts groups into tiers (root, children of root, grandchildren, etc.)
        2. Imports each tier in parallel using _import_parallel
        3. Injects 'parent' field so relationships are created immediately

        Args:
            groups: List of inventory group data
            progress_callback: Optional callback for progress updates

        Returns:
            List of created inventory group data
        """
        if not groups:
            return []

        # Sort groups into tiers (list of lists)
        # Tier 0: Roots
        # Tier 1: Children of Tier 0
        # ...
        group_tiers = self._topological_sort_tiers(groups)

        all_results = []
        total_success = 0
        total_failed = 0
        total_skipped = 0

        logger.info(
            "importing_inventory_groups_tiered",
            total_groups=len(groups),
            num_tiers=len(group_tiers),
            tier_sizes=[len(tier) for tier in group_tiers],
        )

        # Create a cumulative progress callback
        def tier_progress_cb(success: int, failed: int, skipped: int) -> None:
            nonlocal total_success, total_failed, total_skipped
            # This callback receives totals for the CURRENT batch/tier
            # We need to accumulate them across tiers for the global progress bar
            # But _import_parallel tracks its own cumulative count from 0.
            # So we need to add the *previous* tiers' totals to the current tier's totals
            if progress_callback:
                progress_callback(
                    total_success + success,
                    total_failed + failed,
                    total_skipped + skipped,
                )

        for i, tier_groups in enumerate(group_tiers):
            logger.info("importing_group_tier", tier=i, count=len(tier_groups))

            # Import this tier in parallel
            results = await self._import_parallel(
                "inventory_groups", tier_groups, progress_callback=tier_progress_cb
            )

            # Accumulate totals for next tier's callback base
            # Count actually returned results (successes)
            tier_success = len([r for r in results if r and not r.get("_skipped")])
            tier_skipped = len([r for r in results if r and r.get("_skipped")])
            # Failed is implicit: size of tier - success - skipped
            # (Assuming _import_parallel returns failures as None)
            tier_failed = len(tier_groups) - tier_success - tier_skipped

            # Let's update the running totals based on the *final* callback values of the tier
            total_success += tier_success
            total_failed += tier_failed
            total_skipped += tier_skipped

            all_results.extend(results)

        return all_results

    def _topological_sort_tiers(self, groups: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Sort groups into dependency tiers for parallel import.

        Optimized O(N) algorithm.

        1. Build adjacency map: parent_id -> [child_ids]
        2. Build parent map: child_id -> parent_id (injects 'parent' field into data)
        3. Identify roots (no parent)
        4. BFS to build tiers

        Args:
            groups: List of inventory group data

        Returns:
            List of lists (tiers), where Tier 0 is roots, Tier 1 is their children, etc.
        """
        # Index groups by ID
        group_by_id = {g.get("_source_id", g.get("id")): g for g in groups}

        # Adjacency list: parent_id -> list of child_ids
        children_map: dict[Any, list[Any]] = {}
        # Parent map: child_id -> parent_id
        parent_map = {}

        # Initialize
        for gid in group_by_id:
            children_map[gid] = []

        # Build graph (O(N) - iterate once)
        for group in groups:
            parent_id = group.get("_source_id", group.get("id"))
            child_ids = group.get("children", [])

            for child_id in child_ids:
                if child_id in group_by_id:  # Only track if child is in this import set
                    children_map[parent_id].append(child_id)
                    parent_map[child_id] = parent_id

                    # INJECT PARENT FIELD!
                    # This enables the importer to link them automatically via DEPENDENCIES
                    group_by_id[child_id]["parent"] = parent_id

            # Remove children list to avoid API errors (cleaned up by importer usually, but good to be safe)
            group.pop("children", None)

        # Identify roots (groups with no parent in this set)
        roots = []
        for gid, group in group_by_id.items():
            if gid not in parent_map:
                roots.append(group)

        # BFS to build tiers
        tiers = []
        current_tier = roots

        visited = set()
        for g in roots:
            visited.add(g.get("_source_id", g.get("id")))

        while current_tier:
            tiers.append(current_tier)
            next_tier = []

            for group in current_tier:
                parent_id = group.get("_source_id", group.get("id"))
                children_ids = children_map.get(parent_id, [])

                for child_id in children_ids:
                    if child_id not in visited:
                        visited.add(child_id)
                        next_tier.append(group_by_id[child_id])

            current_tier = next_tier

        # Check for circular dependencies or orphaned loops
        if len(visited) != len(groups):
            missing_count = len(groups) - len(visited)
            logger.warning(
                "circular_dependency_detected",
                total_groups=len(groups),
                visited_groups=len(visited),
                missing=missing_count,
                message="Some groups were skipped due to circular dependencies or disconnection",
            )
            # We could raise ValueError, or just log warning and return what we have.
            # Returning what we have is safer for partial success.

        return tiers


class InventorySourceImporter(ResourceImporter):
    """Importer for inventory source resources.

    Inventory sources can have multiple dependencies:
    - inventory (required)
    - source_project (optional, for SCM sources)
    - credential (optional, for authentication)
    - execution_environment (optional, for custom execution environments)
    """

    DEPENDENCIES: dict[str, str] = {
        "inventory": "inventories",
        "source_project": "projects",
        "credential": "credentials",
        "execution_environment": "execution_environments",
    }

    async def import_inventory_sources(
        self,
        sources: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple inventory sources concurrently with live progress updates.

        Handles multiple dependencies (inventory, project, credential).
        Preserves source configuration (source_vars, update options, etc.).

        Args:
            sources: List of inventory source data
            progress_callback: Optional callback for progress updates.
                Called after each inventory source with (success_count, failed_count).

        Returns:
            List of created inventory source data
        """
        # Extract schedules before import
        sources_with_schedules = []
        for source in sources:
            schedules = source.pop("schedules", None)
            if schedules:
                source_id = source.get("_source_id", source.get("id"))
                sources_with_schedules.append(
                    {
                        "source_inventory_source_id": source_id,
                        "schedules": schedules,
                    }
                )

        # Import inventory sources
        results = await self._import_parallel("inventory_sources", sources, progress_callback)

        # Import schedules for successfully imported inventory sources
        if sources_with_schedules:
            logger.info(
                "importing_inventory_source_schedules",
                total_sources_with_schedules=len(sources_with_schedules),
            )

            for schedule_data in sources_with_schedules:
                source_inventory_source_id = schedule_data["source_inventory_source_id"]
                schedules = schedule_data["schedules"]

                # Get the target inventory source ID from the state mapping
                target_inventory_source_id = self.state.get_mapped_id(
                    "inventory_sources", source_inventory_source_id
                )
                if not target_inventory_source_id:
                    logger.warning(
                        "inventory_source_not_found_for_schedule",
                        source_inventory_source_id=source_inventory_source_id,
                    )
                    continue

                # Get inventory source name for logging
                source_result = next(
                    (s for s in results if s.get("id") == target_inventory_source_id), None
                )
                source_name = source_result.get("name", "unknown") if source_result else "unknown"

                for schedule in schedules:
                    schedule_name = schedule.get("name", "unknown")
                    # Capture source schedule ID before it's removed (for database tracking)
                    source_schedule_id = schedule.get("id")

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

                    # SAFETY: Disable schedule by default to prevent automatic execution
                    original_enabled = schedule_to_import.get("enabled", True)
                    schedule_to_import["enabled"] = False

                    try:
                        result = await self.client.post(
                            f"inventory_sources/{target_inventory_source_id}/schedules/",
                            json_data=schedule_to_import,
                        )
                        logger.info(
                            "inventory_source_schedule_imported",
                            inventory_source_id=target_inventory_source_id,
                            inventory_source_name=source_name,
                            schedule_name=schedule_name,
                            schedule_id=result.get("id"),
                            original_enabled=original_enabled,
                            imported_as_disabled=True,
                        )

                        # Track schedule in database if source_id is available
                        # This allows standalone schedule import to skip already-created schedules
                        sched_tgt_id = result.get("id")
                        if source_schedule_id and sched_tgt_id is not None:
                            try:
                                self.state.save_id_mapping(
                                    resource_type="schedules",
                                    source_id=int(source_schedule_id),
                                    target_id=int(sched_tgt_id),
                                    source_name=schedule_name,
                                    target_name=schedule_name,
                                )
                                self.state.mark_completed(
                                    resource_type="schedules",
                                    source_id=int(source_schedule_id),
                                    target_id=int(sched_tgt_id),
                                    target_name=schedule_name,
                                    source_name=schedule_name,
                                )
                                logger.debug(
                                    "inventory_source_schedule_tracked",
                                    source_id=source_schedule_id,
                                    target_id=int(sched_tgt_id),
                                    schedule_name=schedule_name,
                                )
                            except Exception as tracking_error:
                                # Don't fail schedule import if tracking fails
                                logger.warning(
                                    "inventory_source_schedule_tracking_failed",
                                    source_id=source_schedule_id,
                                    target_id=int(sched_tgt_id),
                                    schedule_name=schedule_name,
                                    error=str(tracking_error),
                                )
                    except Exception as e:
                        logger.error(
                            "inventory_source_schedule_import_failed",
                            inventory_source_id=target_inventory_source_id,
                            inventory_source_name=source_name,
                            schedule_name=schedule_name,
                            error=str(e),
                        )

        # Automatically trigger sync for all successfully imported inventory sources
        if results:
            logger.info(
                "triggering_inventory_source_syncs",
                total_inventory_sources=len(results),
            )

            for result in results:
                inventory_source_id = result.get("id")
                inventory_source_name = result.get("name", "unknown")

                if not inventory_source_id:
                    continue

                try:
                    # Trigger sync via POST to /inventory_sources/{id}/update/
                    sync_result = await self.client.post(
                        f"inventory_sources/{inventory_source_id}/update/",
                        json_data={},
                    )
                    logger.info(
                        "inventory_source_sync_triggered",
                        inventory_source_id=inventory_source_id,
                        inventory_source_name=inventory_source_name,
                        inventory_update_id=sync_result.get("id"),
                    )
                except Exception as e:
                    logger.warning(
                        "inventory_source_sync_failed",
                        inventory_source_id=inventory_source_id,
                        inventory_source_name=inventory_source_name,
                        error=str(e),
                        hint="Check inventory source manually for outdated EE's which are pointing to older AAP-2.4 automation hub address",
                    )

        return results
