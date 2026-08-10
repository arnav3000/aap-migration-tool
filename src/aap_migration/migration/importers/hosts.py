from collections.abc import Callable
from typing import Any

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.client.bulk_operations import BulkOperations
from aap_migration.client.exceptions import APIError
from aap_migration.config import PerformanceConfig
from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.migration.state import MigrationState
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class HostImporter(ResourceImporter):
    """Importer for host resources with bulk operations support."""

    DEPENDENCIES: dict[str, str] = {
        "inventory": "inventories",
    }

    def __init__(
        self,
        client: AAPTargetClient,
        state: MigrationState,
        performance_config: PerformanceConfig,
        resource_mappings: dict[str, dict[str, str]] | None = None,
        name_prefix: str = "",
    ):
        """Initialize host importer with bulk operations.

        Args:
            client: AAP target client instance
            state: Migration state manager
            performance_config: Performance configuration
            resource_mappings: Optional resource name mappings from config/mappings.yaml
            name_prefix: Optional source name prefix
        """
        super().__init__(
            client, state, performance_config, resource_mappings, name_prefix=name_prefix
        )
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

            # Fetch existing hosts in this inventory to check for duplicates (paginated)
            existing_hosts_by_name: dict[str, dict[str, Any]] = {}
            page = 1
            while True:
                existing_hosts_data = await self.client.get(
                    f"inventories/{inventory_id}/hosts/",
                    params={"page_size": 200, "page": page},
                )
                for host_entry in existing_hosts_data.get("results", []):
                    existing_hosts_by_name[host_entry["name"]] = host_entry
                if not existing_hosts_data.get("next"):
                    break
                page += 1

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

                # Batch save ID mappings for all created hosts (match by name, not batch index)
                if created_hosts and source_info:
                    created_by_name = {host["name"]: host for host in created_hosts}
                    mappings = []
                    for info in source_info:
                        created_host = created_by_name.get(info["source_name"])
                        if created_host:
                            mappings.append(
                                {
                                    "resource_type": "hosts",
                                    "source_id": info["source_id"],
                                    "target_id": created_host["id"],
                                    "source_name": info["source_name"],
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

                # Mark failed in state (ensure progress row exists first)
                for source_id in source_ids:
                    source_name = source_name_by_id.get(source_id, f"host_{source_id}")
                    if not self.state.has_source_mapping("hosts", source_id):
                        self.state.create_source_mapping(
                            "hosts", source_id, source_name=source_name
                        )
                    self.state.mark_in_progress(
                        resource_type="hosts",
                        source_id=source_id,
                        source_name=source_name,
                        phase="import",
                    )
                    self.state.mark_failed("hosts", source_id, str(e))

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

    DEPENDENCIES: dict[str, str] = {
        "host_id": "hosts",
        "inventory_id": "inventories",
    }

    async def import_resource(  # type: ignore[override]
        self,
        resource: dict[str, Any],
        xformed: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Import a single host-inventory membership.

        Args:
            resource: Original membership data with host_id and inventory_id
            xformed: Not used for memberships (no transformation needed)

        Returns:
            Result dictionary with status
        """
        source_host_id = resource.get("host_id")
        source_inventory_id = resource.get("inventory_id")
        host_name = resource.get("host_name", f"host_{source_host_id}")
        inventory_name = resource.get("inventory_name", f"inventory_{source_inventory_id}")

        if source_host_id is None or source_inventory_id is None:
            logger.warning(
                "membership_missing_source_ids",
                host_name=host_name,
                inventory_name=inventory_name,
                message="Skipping membership without host_id and inventory_id",
            )
            self.stats["skipped_count"] += 1
            return {"status": "skipped", "reason": "missing_source_ids"}

        source_membership_id = (int(source_host_id) << 32) | int(source_inventory_id)

        # Check if already imported
        if self.state.is_migrated("host_inventory_memberships", source_membership_id):
            logger.debug(
                "membership_already_migrated",
                host_id=source_host_id,
                inventory_id=source_inventory_id,
                message="Membership already migrated, skipping",
            )
            self.stats["skipped_count"] += 1
            return {"status": "skipped", "reason": "already_migrated"}

        # Map source IDs to target IDs
        target_host_id = self.state.get_mapped_id("hosts", int(source_host_id))
        target_inventory_id = self.state.get_mapped_id("inventories", int(source_inventory_id))

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

        target_membership_id = (int(target_host_id) << 32) | int(target_inventory_id)

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
                    source_membership_id,
                    source_name=f"{host_name} -> {inventory_name}",
                )
                self.state.mark_completed(
                    resource_type="host_inventory_memberships",
                    source_id=source_membership_id,
                    target_id=target_membership_id,
                    source_name=f"{host_name} -> {inventory_name}",
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
                    source_membership_id,
                    source_name=f"{host_name} -> {inventory_name}",
                )
                self.state.mark_completed(
                    resource_type="host_inventory_memberships",
                    source_id=source_membership_id,
                    target_id=target_membership_id,
                    source_name=f"{host_name} -> {inventory_name}",
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
                source_membership_id,
                source_name=f"{host_name} -> {inventory_name}",
            )
            self.state.mark_completed(
                resource_type="host_inventory_memberships",
                source_id=source_membership_id,
                target_id=target_membership_id,
                source_name=f"{host_name} -> {inventory_name}",
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
                source_membership_id,
                source_name=f"{host_name} -> {inventory_name}",
            )
            self.state.mark_failed(
                resource_type="host_inventory_memberships",
                source_id=source_membership_id,
                error_message=error_msg,
            )
            self.stats["error_count"] += 1

            self.import_errors.append(
                {
                    "resource_type": "host_inventory_memberships",
                    "source_id": source_membership_id,
                    "name": f"{host_name} -> {inventory_name}",
                    "error": error_msg,
                    "error_type": type(e).__name__,
                }
            )

            return {"status": "failed", "error": error_msg}
