"""Domain resource importers (split from migration.importer; re-exported)."""

from collections.abc import Callable
from typing import Any, cast

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.client.bulk_operations import BulkOperations
from aap_migration.client.exceptions import APIError
from aap_migration.config import PerformanceConfig
from aap_migration.migration.importers.base import (
    ResourceImporter,
    logger,
)
from aap_migration.migration.state import MigrationState


class ExecutionEnvironmentImporter(ResourceImporter):
    """Importer for execution environment resources.

    Execution Environments are container images that provide the Ansible
    runtime environment. They depend on:
    - organization (required)
    - credential (optional, for private registries)
    """

    DEPENDENCIES = {
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


class RBACImporter(ResourceImporter):
    """Importer for RBAC (Role-Based Access Control) role assignments.

    Handles granting roles to users and teams on various resource types.
    Does not have traditional dependencies as it operates on already-imported resources.
    """

    async def import_role_assignments(
        self, assignments: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Import RBAC role assignments.

        Each assignment grants a specific role to a user or team on a resource.

        Args:
            assignments: List of role assignment data with structure:
                {
                    "resource_type": "organizations",
                    "resource_id": 123,  # Source resource ID
                    "role": "admin",
                    "user": 456,  # Source user ID (mutually exclusive with team)
                    "team": 789,  # Source team ID (mutually exclusive with user)
                }

        Returns:
            List of successfully granted role assignment data
        """
        results = []

        for assignment in assignments:
            try:
                resource_type = assignment["resource_type"]
                source_resource_id = assignment["resource_id"]
                role_name = assignment["role"]
                source_user_id = assignment.get("user")
                source_team_id = assignment.get("team")

                # Resolve resource ID
                target_resource_id = self.state.get_mapped_id(resource_type, source_resource_id)
                if not target_resource_id:
                    logger.warning(
                        "rbac_resource_not_found",
                        resource_type=resource_type,
                        source_id=source_resource_id,
                    )
                    continue

                # Resolve user or team ID (prefer user if both are present)
                if source_user_id:
                    target_principal_id = self.state.get_mapped_id("users", source_user_id)
                    if not target_principal_id:
                        logger.warning(
                            "rbac_user_not_found",
                            source_user_id=source_user_id,
                        )
                        continue
                    principal_key = "user"
                    principal_id = target_principal_id
                elif source_team_id:
                    target_principal_id = self.state.get_mapped_id("teams", source_team_id)
                    if not target_principal_id:
                        logger.warning(
                            "rbac_team_not_found",
                            source_team_id=source_team_id,
                        )
                        continue
                    principal_key = "team"
                    principal_id = target_principal_id
                else:
                    logger.warning(
                        "rbac_no_principal",
                        resource_type=resource_type,
                        source_resource_id=source_resource_id,
                        assignment=assignment,
                    )
                    continue

                # Grant role via AAP API
                # Endpoint format: {resource_type}/{id}/roles/{role_name}/{principal_type}s/
                principal_type_plural = f"{principal_key}s"
                endpoint = f"{resource_type}/{target_resource_id}/roles/{role_name}/{principal_type_plural}/"
                data = {"id": principal_id}

                logger.info(
                    "granting_role",
                    resource_type=resource_type,
                    source_resource_id=source_resource_id,
                    target_resource_id=target_resource_id,
                    role=role_name,
                    principal_type=principal_key,
                    source_principal_id=source_user_id or source_team_id,
                    target_principal_id=principal_id,
                )

                result = await self.client.post(
                    endpoint=endpoint,
                    data=data,
                )

                results.append(result)
                self.stats["imported_count"] += 1

            except Exception as e:
                logger.error(
                    "rbac_import_error",
                    resource_type=resource_type,
                    source_resource_id=source_resource_id,
                    role=role_name,
                    assignment=assignment,
                    error=str(e),
                )
                self.stats["error_count"] += 1

                # Track error for reporting
                self.import_errors.append(
                    {
                        "resource_type": "rbac_assignments",
                        "source_id": source_resource_id,
                        "name": f"{resource_type}/{role_name}",
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "details": assignment,
                    }
                )

                continue

        return results


class HostImporter(ResourceImporter):
    """Importer for host resources with bulk operations support."""

    DEPENDENCIES = {
        "inventory": "inventories",
    }

    def __init__(
        self,
        client: AAPTargetClient,
        state: MigrationState,
        performance_config: PerformanceConfig,
        resource_mappings: dict[str, dict[str, str]] | None = None,
    ):
        """Initialize host importer with bulk operations.

        Args:
            client: AAP target client instance
            state: Migration state manager
            performance_config: Performance configuration
            resource_mappings: Optional resource name mappings from config/mappings.yaml
        """
        super().__init__(client, state, performance_config, resource_mappings)
        self.bulk_ops = BulkOperations(client, performance_config)

    async def import_hosts_bulk(
        self,
        inventory_id: int,
        hosts: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Import hosts using bulk API for performance.

        Processes batches sequentially for reliable progress tracking.

        Args:
            inventory_id: Target inventory ID
            hosts: List of host data

        Returns:
            Bulk operation result with total_created, total_failed, total_skipped
        """
        batch_size = self.performance_config.batch_sizes.get("hosts", 200)

        logger.info(
            "bulk_import_hosts_starting",
            inventory_id=inventory_id,
            host_count=len(hosts),
            batch_size=batch_size,
        )

        all_results = []
        total_created = 0
        total_failed = 0
        total_skipped = 0

        # Split into chunks
        chunks = [hosts[i : i + batch_size] for i in range(0, len(hosts), batch_size)]

        # Process batches sequentially for reliable progress tracking
        for batch_idx, batch in enumerate(chunks):
            # Prepare host data for bulk API
            prepared_hosts = []
            source_ids = []
            source_info: list[dict] = []
            source_name_by_id: dict[int, str] = {}
            batch_skipped = 0

            # Fetch existing hosts in this inventory to check for duplicates
            existing_hosts_data = await self.client.get(
                f"inventories/{inventory_id}/hosts/",
                params={"page_size": 1000},  # Get many hosts to check duplicates
            )
            existing_hosts_by_name = {h["name"]: h for h in existing_hosts_data.get("results", [])}

            for host in batch:
                source_id = host.pop("_source_id", host.get("id"))
                source_name = host.get("name", f"host_{source_id}")
                source_name_by_id[source_id] = source_name

                # Skip if already migrated
                if self.state.is_migrated("hosts", source_id):
                    self.stats["skipped_count"] += 1
                    batch_skipped += 1
                    continue

                # Check if host already exists in target inventory (by name)
                if source_name in existing_hosts_by_name:
                    existing_host = existing_hosts_by_name[source_name]
                    # Create ID mapping for existing host
                    self.state.save_id_mapping(
                        resource_type="hosts",
                        source_id=source_id,
                        target_id=existing_host["id"],
                        source_name=source_name,
                        target_name=existing_host.get("name"),
                    )
                    # Mark as completed to track this resource was processed
                    self.state.mark_completed(
                        resource_type="hosts",
                        source_id=source_id,
                        target_id=existing_host["id"],
                        target_name=existing_host.get("name"),
                        source_name=source_name,  # Auto-creates record if missing
                    )
                    logger.info(
                        "host_already_exists",
                        source_id=source_id,
                        source_name=source_name,
                        target_id=existing_host["id"],
                        inventory_id=inventory_id,
                        message="Host already exists in target inventory - mapped existing host",
                    )
                    self.stats["conflict_count"] += 1
                    batch_skipped += 1
                    continue

                source_ids.append(source_id)
                source_info.append(
                    {
                        "source_id": source_id,
                        "source_name": source_name,
                    }
                )

                prepared_hosts.append(
                    {
                        "name": host["name"],
                        "description": host.get("description", ""),
                        "enabled": host.get("enabled", True),
                        "variables": host.get("variables", {}),
                        "inventory": inventory_id,
                    }
                )

            if batch_skipped > 0:
                total_skipped += batch_skipped

            if not prepared_hosts:
                continue

            try:
                result = await self.bulk_ops.bulk_create_hosts(
                    inventory_id=inventory_id,
                    hosts=prepared_hosts,
                    batch_size=batch_size,
                )

                created_hosts = result.get("hosts", [])
                failed_hosts = result.get("failed", [])

                # Batch save ID mappings for all created hosts
                if created_hosts and source_info:
                    mappings = []
                    for idx, created_host in enumerate(created_hosts):
                        if idx < len(source_info):
                            mappings.append(
                                {
                                    "resource_type": "hosts",
                                    "source_id": source_info[idx]["source_id"],
                                    "target_id": created_host["id"],
                                    "source_name": source_info[idx]["source_name"],
                                    "target_name": created_host.get("name"),
                                }
                            )

                    self.state.batch_create_mappings(mappings)

                created_count = len(created_hosts)
                failed_count = len(failed_hosts)

                total_created += created_count
                total_failed += failed_count

                self.stats["imported_count"] += created_count
                self.stats["error_count"] += failed_count

                all_results.append(result)

                # Report progress after batch
                if progress_callback:
                    progress_callback(total_created, total_failed, total_skipped)

            except Exception as e:
                logger.error(
                    "bulk_import_batch_failed",
                    resource_type="hosts",
                    inventory_id=inventory_id,
                    batch_idx=batch_idx,
                    error=str(e),
                )

                # Mark failed in state
                for source_id in source_ids:
                    source_name = source_name_by_id.get(source_id, f"host_{source_id}")
                    try:
                        if not self.state.has_source_mapping("hosts", source_id):
                            self.state.create_source_mapping(
                                "hosts", source_id, source_name=source_name
                            )
                        self.state.mark_failed("hosts", source_id, str(e), source_name=source_name)
                    except Exception as state_error:
                        logger.error(
                            "mark_failed_state_error",
                            resource_type="hosts",
                            source_id=source_id,
                            error=str(state_error),
                        )

                self.stats["error_count"] += len(source_ids)
                total_failed += len(source_ids)

                self.import_errors.append(
                    {
                        "resource_type": "hosts",
                        "source_id": f"batch_{batch_idx}",
                        "name": f"batch {batch_idx} of {len(source_ids)} hosts",
                        "error": str(e),
                        "error_type": type(e).__name__,
                    }
                )
                # Continue with next batch instead of failing completely

                # Report progress even after failure
                if progress_callback:
                    progress_callback(total_created, total_failed, total_skipped)

        logger.info(
            "bulk_import_hosts_completed",
            inventory_id=inventory_id,
            total_hosts=len(hosts),
            created=total_created,
            failed=total_failed,
            skipped=total_skipped,
        )

        return {
            "total_requested": len(hosts),
            "total_created": total_created,
            "total_failed": total_failed,
            "total_skipped": total_skipped,
            "results": all_results,
        }


class HostInventoryMembershipImporter(ResourceImporter):
    """Importer for host-inventory membership relationships.

    This importer restores host memberships in multiple inventories by adding
    hosts to additional inventories beyond their primary inventory. Only
    processes memberships for regular inventories.
    """

    DEPENDENCIES = {
        "host_id": "hosts",
        "inventory_id": "inventories",
    }

    async def import_resource(
        self,
        resource_type: str | dict[str, Any],
        source_id: int | dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        resolve_dependencies: bool = True,
        xformed: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Import a single host-inventory membership.

        Args:
            resource: Original membership data with host_id and inventory_id
            xformed: Not used for memberships (no transformation needed)

        Returns:
            Result dictionary with status
        """
        resource: dict[str, Any] = cast(dict[str, Any], resource_type)
        source_host_id = resource.get("host_id")
        source_inventory_id = resource.get("inventory_id")
        host_name = resource.get("host_name", f"host_{source_host_id}")
        inventory_name = resource.get("inventory_name", f"inventory_{source_inventory_id}")

        # Generate a unique identifier for this membership
        membership_id = f"{source_host_id}_{source_inventory_id}"

        # Check if already imported
        if self.state.is_migrated("host_inventory_memberships", cast(int, membership_id)):
            logger.debug(
                "membership_already_migrated",
                host_id=source_host_id,
                inventory_id=source_inventory_id,
                message="Membership already migrated, skipping",
            )
            self.stats["skipped_count"] += 1
            return {"status": "skipped", "reason": "already_migrated"}

        # Map source IDs to target IDs
        target_host_id = self.state.get_mapped_id("hosts", cast(int, source_host_id))
        target_inventory_id = self.state.get_mapped_id(
            "inventories", cast(int, source_inventory_id)
        )

        if not target_host_id:
            logger.warning(
                "membership_import_host_not_found",
                source_host_id=source_host_id,
                host_name=host_name,
                message="Host not found in target, skipping membership",
            )
            self.stats["skipped_count"] += 1
            return {"status": "skipped", "reason": "host_not_found"}

        if not target_inventory_id:
            logger.warning(
                "membership_import_inventory_not_found",
                source_inventory_id=source_inventory_id,
                inventory_name=inventory_name,
                message="Inventory not found in target, skipping membership",
            )
            self.stats["skipped_count"] += 1
            return {"status": "skipped", "reason": "inventory_not_found"}

        source_label = f"{host_name} -> {inventory_name}"
        self.state.mark_in_progress(
            resource_type="host_inventory_memberships",
            source_id=cast(int, membership_id),
            source_name=source_label,
            phase="import",
        )

        # Check if host is already in this inventory
        try:
            # Get host details to check current inventory
            host_data = await self.client.get(f"hosts/{target_host_id}/")
            primary_inventory_id = host_data.get("inventory")

            # If this is the primary inventory, skip (already set during host import)
            if primary_inventory_id == target_inventory_id:
                logger.debug(
                    "membership_is_primary",
                    host_id=target_host_id,
                    inventory_id=target_inventory_id,
                    message="Host already in this inventory as primary, skipping",
                )
                self.state.create_source_mapping(
                    "host_inventory_memberships",
                    cast(int, membership_id),
                    source_name=source_label,
                )
                self.state.mark_completed(
                    "host_inventory_memberships",
                    cast(int, membership_id),
                    cast(int, f"{target_host_id}_{target_inventory_id}"),
                    source_name=source_label,
                )
                self.stats["skipped_count"] += 1
                return {"status": "skipped", "reason": "already_primary_inventory"}

            # Check if host is already in this inventory (as additional membership)
            existing_hosts = await self.client.get(
                f"inventories/{target_inventory_id}/hosts/",
                params={"id": target_host_id, "page_size": 1},
            )

            if existing_hosts.get("count", 0) > 0:
                logger.debug(
                    "membership_already_exists",
                    host_id=target_host_id,
                    inventory_id=target_inventory_id,
                    message="Host already in this inventory, skipping",
                )
                self.state.create_source_mapping(
                    "host_inventory_memberships",
                    cast(int, membership_id),
                    source_name=source_label,
                )
                self.state.mark_completed(
                    "host_inventory_memberships",
                    cast(int, membership_id),
                    cast(int, f"{target_host_id}_{target_inventory_id}"),
                    source_name=source_label,
                )
                self.stats["skipped_count"] += 1
                return {"status": "skipped", "reason": "already_in_inventory"}

            # Add host to inventory
            logger.info(
                "adding_host_to_inventory",
                host_id=target_host_id,
                host_name=host_name,
                inventory_id=target_inventory_id,
                inventory_name=inventory_name,
            )

            await self.client.post(
                f"inventories/{target_inventory_id}/hosts/",
                json_data={"id": target_host_id},
            )

            # Mark as successful
            self.state.create_source_mapping(
                "host_inventory_memberships",
                cast(int, membership_id),
                source_name=source_label,
            )
            self.state.mark_completed(
                "host_inventory_memberships",
                cast(int, membership_id),
                cast(int, f"{target_host_id}_{target_inventory_id}"),
                source_name=source_label,
            )
            self.stats["imported_count"] += 1

            logger.info(
                "membership_imported",
                host_name=host_name,
                inventory_name=inventory_name,
                message=f"Added host '{host_name}' to inventory '{inventory_name}'",
            )

            return {"status": "created", "target_id": f"{target_host_id}_{target_inventory_id}"}

        except APIError as e:
            error_msg = str(e)
            logger.error(
                "membership_import_failed",
                host_id=source_host_id,
                inventory_id=source_inventory_id,
                error=error_msg,
            )

            self.state.create_source_mapping(
                "host_inventory_memberships",
                cast(int, membership_id),
                source_name=source_label,
            )
            try:
                self.state.mark_failed(
                    "host_inventory_memberships",
                    cast(int, membership_id),
                    error_msg,
                    source_name=source_label,
                )
            except Exception as state_error:
                logger.error(
                    "mark_failed_state_error",
                    resource_type="host_inventory_memberships",
                    source_id=membership_id,
                    error=str(state_error),
                )
            self.stats["error_count"] += 1

            self.import_errors.append(
                {
                    "resource_type": "host_inventory_memberships",
                    "source_id": membership_id,
                    "name": source_label,
                    "error": error_msg,
                    "error_type": type(e).__name__,
                }
            )

            return {"status": "failed", "error": error_msg}

    async def import_host_inventory_memberships(
        self,
        memberships: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import all host-inventory memberships.

        Args:
            memberships: List of membership dicts with host_id and inventory_id
            progress_callback: Optional (success, failed, skipped) callback

        Returns:
            List of result dicts
        """
        if not memberships:
            return []

        results = []
        success, failed, skipped = 0, 0, 0

        for membership in memberships:
            try:
                result = await self.import_resource(membership)
            except Exception as e:
                logger.error(
                    "membership_import_unexpected_error",
                    resource_type="host_inventory_memberships",
                    host_id=membership.get("host_id"),
                    inventory_id=membership.get("inventory_id"),
                    error=str(e),
                )
                result = {"status": "failed", "error": str(e)}
            results.append(result)

            status = result.get("status", "failed")
            if status in ("created",):
                success += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1

            if progress_callback:
                progress_callback(success, failed, skipped)

        return results


class HostGroupMembershipImporter(ResourceImporter):
    """Importer for host-group membership relationships.

    For each (group_id, host_id) pair, adds the host to the group on target
    via POST groups/{target_group_id}/hosts/ with {"id": target_host_id}.
    """

    DEPENDENCIES = {
        "group_id": "inventory_groups",
        "host_id": "hosts",
    }

    async def import_resource(
        self,
        resource_type: str | dict[str, Any],
        source_id: int | dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        resolve_dependencies: bool = True,
        xformed: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Import a single host-group membership.

        Args:
            resource: Membership data with group_id and host_id
            xformed: Not used for memberships (no transformation needed)

        Returns:
            Result dictionary with status
        """
        resource: dict[str, Any] = cast(dict[str, Any], resource_type)
        source_group_id = resource.get("group_id")
        source_host_id = resource.get("host_id")
        group_name = resource.get("group_name", f"group_{source_group_id}")
        host_name = resource.get("host_name", f"host_{source_host_id}")

        membership_id = f"{source_group_id}_{source_host_id}"

        if self.state.is_migrated("host_group_memberships", cast(int, membership_id)):
            logger.debug(
                "group_membership_already_migrated",
                group_id=source_group_id,
                host_id=source_host_id,
                message="Group membership already migrated, skipping",
            )
            self.stats["skipped_count"] += 1
            return {"status": "skipped", "reason": "already_migrated"}

        target_group_id = self.state.get_mapped_id("inventory_groups", cast(int, source_group_id))
        if not target_group_id:
            logger.warning(
                "group_membership_group_not_found",
                source_group_id=source_group_id,
                group_name=group_name,
                message="Group not found in target, skipping membership",
            )
            self.stats["skipped_count"] += 1
            return {"status": "skipped", "reason": "group_not_found"}

        target_host_id = self.state.get_mapped_id("hosts", cast(int, source_host_id))
        if not target_host_id:
            logger.warning(
                "group_membership_host_not_found",
                source_host_id=source_host_id,
                host_name=host_name,
                message="Host not found in target, skipping membership",
            )
            self.stats["skipped_count"] += 1
            return {"status": "skipped", "reason": "host_not_found"}

        source_label = f"{host_name} -> {group_name}"
        self.state.mark_in_progress(
            resource_type="host_group_memberships",
            source_id=cast(int, membership_id),
            source_name=source_label,
            phase="import",
        )

        try:
            existing = await self.client.get(
                f"groups/{target_group_id}/hosts/",
                params={"id": target_host_id, "page_size": 1},
            )
            if existing.get("count", 0) > 0:
                logger.debug(
                    "group_membership_already_exists",
                    host_id=target_host_id,
                    group_id=target_group_id,
                    message="Host already in group, skipping",
                )
                self.state.create_source_mapping(
                    "host_group_memberships",
                    cast(int, membership_id),
                    source_name=source_label,
                )
                self.state.mark_completed(
                    "host_group_memberships",
                    cast(int, membership_id),
                    cast(int, f"{target_group_id}_{target_host_id}"),
                    source_name=source_label,
                )
                self.stats["skipped_count"] += 1
                return {"status": "skipped", "reason": "already_in_group"}

            # Verify host is in the group's inventory before POSTing membership.
            # AWX returns 400 "Host matching query does not exist" otherwise.
            group_inventory_id = None
            source_inventory_id = resource.get("inventory_id")
            if source_inventory_id:
                group_inventory_id = self.state.get_mapped_id("inventories", source_inventory_id)
            if not group_inventory_id:
                group_data = await self.client.get(f"groups/{target_group_id}/")
                group_inventory_id = group_data.get("inventory")

            host_in_inventory = False
            if group_inventory_id:
                host_data = await self.client.get(f"hosts/{target_host_id}/")
                if host_data.get("inventory") == group_inventory_id:
                    host_in_inventory = True
                else:
                    inv_hosts = await self.client.get(
                        f"inventories/{group_inventory_id}/hosts/",
                        params={"id": target_host_id, "page_size": 1},
                    )
                    host_in_inventory = inv_hosts.get("count", 0) > 0

            if not host_in_inventory:
                error_msg = (
                    "host_not_in_group_inventory: host is not a member of the "
                    f"group's inventory (group_inventory_id={group_inventory_id}, "
                    f"host_id={target_host_id})"
                )
                logger.warning(
                    "group_membership_host_not_in_inventory",
                    host_id=target_host_id,
                    host_name=host_name,
                    group_id=target_group_id,
                    group_name=group_name,
                    group_inventory_id=group_inventory_id,
                    message=error_msg,
                )
                self.state.create_source_mapping(
                    "host_group_memberships",
                    cast(int, membership_id),
                    source_name=source_label,
                )
                try:
                    self.state.mark_failed(
                        "host_group_memberships",
                        cast(int, membership_id),
                        error_msg,
                        source_name=source_label,
                    )
                except Exception as state_error:
                    logger.error(
                        "mark_failed_state_error",
                        resource_type="host_group_memberships",
                        source_id=membership_id,
                        error=str(state_error),
                    )
                self.stats["error_count"] += 1
                self.import_errors.append(
                    {
                        "resource_type": "host_group_memberships",
                        "source_id": membership_id,
                        "name": source_label,
                        "error": error_msg,
                        "error_type": "host_not_in_group_inventory",
                    }
                )
                return {
                    "status": "failed",
                    "error": error_msg,
                    "reason": "host_not_in_group_inventory",
                }

            logger.info(
                "adding_host_to_group",
                host_id=target_host_id,
                host_name=host_name,
                group_id=target_group_id,
                group_name=group_name,
            )

            await self.client.post(
                f"groups/{target_group_id}/hosts/",
                json_data={"id": target_host_id},
            )

            self.state.create_source_mapping(
                "host_group_memberships",
                cast(int, membership_id),
                source_name=source_label,
            )
            self.state.mark_completed(
                "host_group_memberships",
                cast(int, membership_id),
                cast(int, f"{target_group_id}_{target_host_id}"),
                source_name=source_label,
            )
            self.stats["imported_count"] += 1

            logger.info(
                "group_membership_imported",
                host_name=host_name,
                group_name=group_name,
                message=f"Added host '{host_name}' to group '{group_name}'",
            )

            return {"status": "created", "target_id": f"{target_group_id}_{target_host_id}"}

        except APIError as e:
            error_msg = str(e)
            logger.error(
                "group_membership_import_failed",
                group_id=source_group_id,
                host_id=source_host_id,
                error=error_msg,
            )

            self.state.create_source_mapping(
                "host_group_memberships",
                cast(int, membership_id),
                source_name=source_label,
            )
            try:
                self.state.mark_failed(
                    "host_group_memberships",
                    cast(int, membership_id),
                    error_msg,
                    source_name=source_label,
                )
            except Exception as state_error:
                logger.error(
                    "mark_failed_state_error",
                    resource_type="host_group_memberships",
                    source_id=membership_id,
                    error=str(state_error),
                )
            self.stats["error_count"] += 1
            self.import_errors.append(
                {
                    "resource_type": "host_group_memberships",
                    "source_id": membership_id,
                    "name": source_label,
                    "error": error_msg,
                    "error_type": type(e).__name__,
                }
            )
            return {"status": "failed", "error": error_msg}

    async def import_host_group_memberships(
        self,
        memberships: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import all host-group memberships.

        Args:
            memberships: List of membership dicts with group_id and host_id
            progress_callback: Optional (success, failed, skipped) callback

        Returns:
            List of result dicts
        """
        if not memberships:
            return []

        results = []
        success, failed, skipped = 0, 0, 0

        for membership in memberships:
            try:
                result = await self.import_resource(membership)
            except Exception as e:
                logger.error(
                    "membership_import_unexpected_error",
                    resource_type="host_group_memberships",
                    group_id=membership.get("group_id"),
                    host_id=membership.get("host_id"),
                    error=str(e),
                )
                result = {"status": "failed", "error": str(e)}
            results.append(result)

            status = result.get("status", "failed")
            if status == "created":
                success += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1

            if progress_callback:
                progress_callback(success, failed, skipped)

        return results
