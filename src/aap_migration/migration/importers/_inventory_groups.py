"""Domain resource importers (split from migration.importer; re-exported)."""

from collections.abc import Callable
from typing import Any, cast

from aap_migration.client.exceptions import APIError
from aap_migration.migration.importers.base import (
    ResourceImporter,
    logger,
)


class InventoryGroupImporter(ResourceImporter):
    """Importer for inventory group resources.

    Handles nested hierarchies via topological sorting to ensure parents
    are imported before children.
    Uses optimized tier-based parallel import for performance.
    """

    DEPENDENCIES = {
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
        """Import inventory group with correct API endpoint and parent-child linking.

        Overrides parent to use 'groups' endpoint instead of 'inventory_groups'.
        After creating the group, establishes parent-child hierarchy via
        POST groups/{parent_id}/children/ since the AAP API does not accept
        a 'parent' field in the group creation payload.
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

            # Pop parent before API call — the AAP API POST /groups/ does not
            # accept a 'parent' field. Parent-child relationships are established
            # separately via POST groups/{parent_id}/children/.
            parent_target_id = data.pop("parent", None)

            # Use correct API endpoint
            result = await self.client.create_resource(
                resource_type=api_resource_type,
                data=data,
                check_exists=True,
            )

            target_id = result.get("id")

            # Establish parent-child relationship via children endpoint
            if parent_target_id and target_id:
                await self._link_parent_child(
                    parent_target_id=parent_target_id,
                    child_target_id=target_id,
                    group_name=data.get("name", "unknown"),
                )

            self.state.mark_completed(
                resource_type=resource_type,
                source_id=source_id,
                target_id=cast(int, target_id),
            )

            self.stats["imported_count"] += 1
            logger.info(
                "resource_imported",
                resource_type=resource_type,
                source_id=source_id,
                target_id=target_id,
                parent_target_id=parent_target_id,
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

    async def _link_parent_child(
        self,
        parent_target_id: int,
        child_target_id: int,
        group_name: str = "unknown",
    ) -> None:
        """Establish parent-child relationship between two groups on the target.

        Calls POST /api/v2/groups/{parent_id}/children/ with {"id": child_id}.
        This is the same pattern used by HostGroupMembershipImporter for
        groups/{id}/hosts/.

        Args:
            parent_target_id: Target ID of the parent group
            child_target_id: Target ID of the child group
            group_name: Name of the child group (for logging)
        """
        try:
            await self.client.post(
                f"groups/{parent_target_id}/children/",
                json_data={"id": child_target_id},
            )
            logger.info(
                "group_parent_child_linked",
                parent_id=parent_target_id,
                child_id=child_target_id,
                child_name=group_name,
                message=f"Linked group '{group_name}' as child of group {parent_target_id}",
            )
        except APIError as e:
            if "already" in str(e).lower():
                logger.debug(
                    "group_parent_child_already_exists",
                    parent_id=parent_target_id,
                    child_id=child_target_id,
                    child_name=group_name,
                    message="Parent-child relationship already exists",
                )
            else:
                logger.warning(
                    "group_parent_child_link_failed",
                    parent_id=parent_target_id,
                    child_id=child_target_id,
                    child_name=group_name,
                    error=str(e),
                    message="Failed to establish parent-child group relationship",
                )

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

    Special handling for auto-created sources:
    - When a constructed inventory is created, AAP auto-generates an inventory
      source named "Auto-created source for: <name>". These sources carry the
      constructed inventory plugin configuration (source_vars, limit).
    - Instead of creating a new source (which fails with "Cannot create Inventory
      Source for Smart or Constructed Inventories"), this importer detects auto-created
      sources and updates the existing auto-created source on the target with
      the source_vars and limit from the exported data.
    """

    DEPENDENCIES = {
        "inventory": "inventories",
        "source_project": "projects",
        "credential": "credentials",
        "execution_environment": "execution_environments",
    }

    # Prefix used by AAP for auto-created constructed inventory sources
    AUTO_CREATED_SOURCE_PREFIX = "Auto-created source for: "

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Import inventory source with special handling for auto-created sources.

        Auto-created sources (from constructed inventories) are detected by their
        name prefix "Auto-created source for: ". Instead of creating them (which
        fails), we find the auto-created source on the target and update it with
        source_vars and limit from the exported data.

        Args:
            resource_type: Type of resource being imported
            source_id: Source resource ID
            data: Transformed resource data
            resolve_dependencies: Whether to resolve FK dependencies

        Returns:
            Created/updated resource data or None if skipped/failed
        """
        source_name = data.get("name", "")

        # Detect auto-created inventory sources for constructed inventories
        if source_name.startswith(self.AUTO_CREATED_SOURCE_PREFIX):
            return await self._handle_auto_created_source(
                resource_type, source_id, data, resolve_dependencies
            )

        # Standard import for non-auto-created sources
        return await super().import_resource(resource_type, source_id, data, resolve_dependencies)

    async def _handle_auto_created_source(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Handle auto-created inventory source for a constructed inventory.

        Instead of creating a new source (which would fail), finds the
        auto-created source on the target and updates it with source_vars
        and limit from the exported data.

        Args:
            resource_type: Type of resource being imported
            source_id: Source resource ID
            data: Transformed resource data
            resolve_dependencies: Whether to resolve FK dependencies

        Returns:
            Updated resource data or None if skipped/failed
        """
        # Check if already imported
        if self.state.is_migrated(resource_type, source_id):
            logger.debug(
                "auto_created_source_already_imported",
                resource_type=resource_type,
                source_id=source_id,
            )
            self.stats["skipped_count"] += 1
            return None

        source_name = data.get("name", "")

        # Mark as in progress
        self.state.mark_in_progress(
            resource_type=resource_type,
            source_id=source_id,
            source_name=source_name,
            phase="import",
        )

        try:
            # Resolve inventory dependency to get target inventory ID
            if resolve_dependencies:
                data = await self._resolve_dependencies(resource_type, data)

            target_inventory_id = data.get("inventory")
            if not target_inventory_id:
                logger.warning(
                    "auto_created_source_missing_inventory",
                    source_id=source_id,
                    source_name=source_name,
                )
                self.stats["error_count"] += 1
                self.state.mark_failed(
                    resource_type=resource_type,
                    source_id=source_id,
                    error_message="Could not resolve inventory for auto-created source",
                )
                return None

            # Find the auto-created source on the target by querying inventory sources
            # for this specific inventory
            try:
                target_sources = await self.client.list_resources(
                    "inventory_sources",
                    filters={"inventory": target_inventory_id},
                )
            except Exception as e:
                logger.error(
                    "auto_created_source_lookup_failed",
                    source_id=source_id,
                    target_inventory_id=target_inventory_id,
                    error=str(e),
                )
                self.stats["error_count"] += 1
                self.state.mark_failed(
                    resource_type=resource_type,
                    source_id=source_id,
                    error_message=f"Failed to look up auto-created source: {str(e)}",
                )
                return None

            # Find the auto-created source (name starts with prefix)
            auto_source = None
            for src in target_sources:
                if src.get("name", "").startswith(self.AUTO_CREATED_SOURCE_PREFIX):
                    auto_source = src
                    break

            if not auto_source:
                logger.warning(
                    "auto_created_source_not_found_on_target",
                    source_id=source_id,
                    source_name=source_name,
                    target_inventory_id=target_inventory_id,
                    message="Auto-created source not found; the inventory may not be constructed",
                )
                # Mark as skipped since we can't create it for constructed inventories
                self.state.mark_skipped(
                    resource_type=resource_type,
                    source_id=source_id,
                    reason=f"Auto-created source not found on target for inventory {target_inventory_id}",
                    source_name=source_name,
                )
                self.stats["skipped_count"] += 1
                return None

            # Build update payload with source_vars and limit from exported data
            update_data = {}
            if "source_vars" in data and data["source_vars"]:
                update_data["source_vars"] = data["source_vars"]
            if "limit" in data and data["limit"]:
                update_data["limit"] = data["limit"]
            if "verbosity" in data:
                update_data["verbosity"] = data["verbosity"]
            if "update_cache_timeout" in data:
                update_data["update_cache_timeout"] = data["update_cache_timeout"]
            if "update_on_launch" in data:
                update_data["update_on_launch"] = data["update_on_launch"]

            if update_data:
                result = await self.client.update_resource(
                    "inventory_sources",
                    auto_source["id"],
                    update_data,
                )
                logger.info(
                    "auto_created_source_updated",
                    source_id=source_id,
                    source_name=source_name,
                    target_id=auto_source["id"],
                    updated_fields=list(update_data.keys()),
                )
            else:
                result = auto_source
                logger.info(
                    "auto_created_source_mapped_no_update",
                    source_id=source_id,
                    source_name=source_name,
                    target_id=auto_source["id"],
                    message="No source_vars or limit to update",
                )

            # Save ID mapping and mark completed
            # Flag auto-created sources for deferred constructed inventory sync.
            # The sync must run AFTER host-group memberships migration so the
            # constructed inventory plugin evaluates against fully populated
            # input inventories (hosts, memberships, and group associations).
            self.state.save_id_mapping(
                resource_type=resource_type,
                source_id=source_id,
                target_id=auto_source["id"],
                source_name=source_name,
                target_name=auto_source.get("name"),
                mapping_metadata={"needs_constructed_sync": True},
            )
            self.state.mark_completed(
                resource_type=resource_type,
                source_id=source_id,
                target_id=auto_source["id"],
                target_name=auto_source.get("name"),
                source_name=source_name,
            )

            self.stats["imported_count"] += 1
            return result

        except Exception as e:
            logger.error(
                "auto_created_source_import_failed",
                source_id=source_id,
                source_name=data.get("name"),
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

                    # Resolve FK fields (source IDs → target IDs)
                    for fk_field, fk_resource_type in [
                        ("inventory", "inventories"),
                        ("execution_environment", "execution_environments"),
                    ]:
                        if schedule_to_import.get(fk_field):
                            target_fk_id = self.state.get_mapped_id(
                                fk_resource_type, schedule_to_import[fk_field]
                            )
                            if target_fk_id:
                                schedule_to_import[fk_field] = target_fk_id

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
                        if source_schedule_id and result.get("id"):
                            try:
                                self.state.save_id_mapping(
                                    resource_type="schedules",
                                    source_id=source_schedule_id,
                                    target_id=cast(int, result.get("id")),
                                    source_name=schedule_name,
                                    target_name=schedule_name,
                                )
                                self.state.mark_completed(
                                    resource_type="schedules",
                                    source_id=source_schedule_id,
                                    target_id=cast(int, result.get("id")),
                                    target_name=schedule_name,
                                    source_name=schedule_name,
                                )
                                logger.debug(
                                    "inventory_source_schedule_tracked",
                                    source_id=source_schedule_id,
                                    target_id=result.get("id"),
                                    schedule_name=schedule_name,
                                )
                            except Exception as tracking_error:
                                # Don't fail schedule import if tracking fails
                                logger.warning(
                                    "inventory_source_schedule_tracking_failed",
                                    source_id=source_schedule_id,
                                    target_id=result.get("id"),
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

        # Automatically trigger sync for successfully imported inventory sources.
        # Auto-created sources (constructed inventories) are DEFERRED — their sync
        # must run after host-group memberships migration so the constructed
        # inventory plugin evaluates against fully populated input inventories
        # (hosts, multi-inventory memberships, and group associations).
        if results:
            deferred_count = 0
            sync_count = 0

            for result in results:
                inventory_source_id = result.get("id")
                inventory_source_name = result.get("name", "unknown")

                if not inventory_source_id:
                    continue

                # Defer sync for auto-created constructed inventory sources.
                # The needs_constructed_sync flag is already persisted to the DB
                # by _handle_auto_created_source(); sync will be triggered after
                # host-group memberships migration via trigger_deferred_constructed_syncs().
                if inventory_source_name.startswith(self.AUTO_CREATED_SOURCE_PREFIX):
                    deferred_count += 1
                    logger.info(
                        "constructed_inventory_sync_deferred",
                        inventory_source_id=inventory_source_id,
                        inventory_source_name=inventory_source_name,
                        reason="Deferred until after hosts migration",
                    )
                    continue

                try:
                    # Trigger sync via POST to /inventory_sources/{id}/update/
                    sync_result = await self.client.post(
                        f"inventory_sources/{inventory_source_id}/update/",
                        json_data={},
                    )
                    sync_count += 1
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

            logger.info(
                "inventory_source_sync_summary",
                total_results=len(results),
                synced_immediately=sync_count,
                deferred_for_hosts=deferred_count,
            )

        return results

    async def trigger_deferred_constructed_syncs(
        self, *, force: bool = False
    ) -> list[dict[str, Any]]:
        """Trigger sync for constructed inventory sources deferred during import.

        Queries the MigrationState DB for inventory sources flagged with
        needs_constructed_sync=True and fires POST /inventory_sources/{id}/update/
        for each. On success, clears the flag. On failure, leaves the flag
        as True so it will be retried on the next run.

        When force=True (used after host_group_memberships), also re-syncs
        migrated auto-created constructed sources whose flag was already
        cleared — e.g. after a premature sync left groups without hosts.

        This method is safe to call multiple times (idempotent). It reads
        entirely from the database, so it works with a fresh importer instance
        — it does not depend on any state from the original import run.

        Should be called AFTER host-group memberships migration to ensure
        constructed inventories compute membership against fully populated
        input inventories (hosts, host-inventory memberships, and host-group
        memberships must all be established).

        Returns:
            List of sync results (dicts with 'id', 'name', 'status').
        """
        pending = self.state.get_pending_constructed_syncs(force=force)

        if not pending:
            logger.info(
                "no_deferred_constructed_syncs",
                message="No constructed inventory sources pending sync",
                force=force,
            )
            return []

        logger.info(
            "triggering_deferred_constructed_syncs",
            total_pending=len(pending),
            force=force,
            message="Triggering constructed inventory syncs (deferred until after hosts migration)",
        )
        sync_results = []
        success_count = 0
        failed_count = 0

        for source_info in pending:
            target_id = source_info["target_id"]
            source_name = source_info.get("source_name", "unknown")
            source_id = source_info["source_id"]

            try:
                sync_result = await self.client.post(
                    f"inventory_sources/{target_id}/update/",
                    json_data={},
                )

                # Clear the flag on success
                self.state.clear_constructed_sync_flag(source_id)

                success_count += 1
                sync_results.append(
                    {
                        "id": target_id,
                        "name": source_name,
                        "status": "synced",
                        "inventory_update_id": sync_result.get("id"),
                    }
                )

                logger.info(
                    "deferred_constructed_sync_triggered",
                    target_id=target_id,
                    source_name=source_name,
                    inventory_update_id=sync_result.get("id"),
                )

            except Exception as e:
                failed_count += 1
                sync_results.append(
                    {
                        "id": target_id,
                        "name": source_name,
                        "status": "failed",
                        "error": str(e),
                    }
                )

                logger.warning(
                    "deferred_constructed_sync_failed",
                    target_id=target_id,
                    source_name=source_name,
                    error=str(e),
                    hint="Flag remains True in DB — will retry on next run",
                )

        logger.info(
            "deferred_constructed_sync_complete",
            total=len(pending),
            success=success_count,
            failed=failed_count,
        )

        return sync_results
