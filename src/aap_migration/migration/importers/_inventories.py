"""Domain resource importers (split from migration.importer; re-exported)."""

from collections.abc import Callable
from typing import Any

from aap_migration.client.exceptions import APIError, ConflictError
from aap_migration.migration.importers.base import (
    ResourceImporter,
    logger,
)


class InventoryImporter(ResourceImporter):
    """Importer for inventory resources.

    Uses a two-pass import strategy:
    - Pass 1: Import regular and smart inventories (kind != "constructed")
    - Pass 2: Import constructed inventories with input_inventories resolved

    This ordering ensures all input inventories have target ID mappings
    before constructed inventories that reference them are created.
    """

    DEPENDENCIES = {
        "organization": "organizations",
    }

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Import a single inventory, with special handling for constructed inventories.

        For constructed inventories (kind="constructed"):
        1. Resolves _input_inventory_ids from source IDs to target IDs
        2. Creates the inventory via POST /constructed_inventories/ with input_inventories
        3. Falls back to standard creation + association if dedicated endpoint fails

        Args:
            resource_type: Type of resource being imported
            source_id: Source resource ID
            data: Transformed resource data
            resolve_dependencies: Whether to resolve FK dependencies

        Returns:
            Created resource data or None if skipped/failed
        """
        is_constructed = data.get("kind") == "constructed"
        # Non-mutating read: keep caller dicts intact for retry passes.
        input_inventory_source_ids = data.get("_input_inventory_ids", []) or []
        data = {k: v for k, v in data.items() if k != "_input_inventory_ids"}

        if not is_constructed:
            return await super().import_resource(
                resource_type, source_id, data, resolve_dependencies
            )

        # --- Constructed inventory handling ---

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
            # Resolve organization dependency
            if resolve_dependencies:
                data = await self._resolve_dependencies(resource_type, data)

            # Resolve input_inventory_ids: map source IDs to target IDs
            target_input_ids = []
            unresolved_inputs = []
            for inv_source_id in input_inventory_source_ids:
                target_id = self.state.get_mapped_id("inventories", inv_source_id)
                if target_id:
                    target_input_ids.append(target_id)
                else:
                    unresolved_inputs.append(inv_source_id)

            if unresolved_inputs:
                logger.warning(
                    "constructed_inventory_unresolved_inputs",
                    resource_type=resource_type,
                    source_id=source_id,
                    source_name=data.get("name"),
                    unresolved_source_ids=unresolved_inputs,
                    resolved_count=len(target_input_ids),
                    message="Some input inventories were not imported; "
                    "constructed inventory will be created with partial inputs",
                )

            # Remove None values
            data = {k: v for k, v in data.items() if v is not None}

            # DUPLICATE DETECTION: Check if already exists in target
            resource_name = data.get("name")
            organization_id = data.get("organization")
            if resource_name and organization_id:
                try:
                    existing = await self.client.find_resource_by_name(
                        resource_type,
                        resource_name,
                        organization_id=organization_id,
                    )
                    if existing:
                        logger.info(
                            "constructed_inventory_exists_updating_inputs",
                            resource_type=resource_type,
                            source_id=source_id,
                            target_id=existing["id"],
                            name=resource_name,
                            input_inventory_count=len(target_input_ids),
                        )
                        # Associate input inventories with existing constructed inventory
                        await self._associate_input_inventories(existing["id"], target_input_ids)
                        self.state.mark_completed(
                            resource_type=resource_type,
                            source_id=source_id,
                            target_id=existing["id"],
                            target_name=existing.get("name"),
                            source_name=resource_name,
                        )
                        self.stats["conflict_count"] += 1
                        return existing
                except Exception as e:
                    logger.debug(
                        "constructed_inventory_duplicate_check_failed",
                        error=str(e),
                        action="continuing_with_create",
                    )

            # Create the constructed inventory with input_inventories
            # Include input_inventories in the POST body
            if target_input_ids:
                data["input_inventories"] = target_input_ids

            logger.info(
                "creating_constructed_inventory",
                source_id=source_id,
                source_name=data.get("name"),
                input_inventory_count=len(target_input_ids),
                target_input_ids=target_input_ids,
            )

            # Create the constructed inventory.
            # Try the dedicated constructed_inventories endpoint first, fall
            # back to the standard inventories endpoint on failure.
            try:
                result = await self.client.create_resource(
                    resource_type="constructed_inventories",
                    data=data,
                    check_exists=True,
                )
            except Exception:
                logger.debug(
                    "constructed_inventories_endpoint_failed_fallback",
                    source_id=source_id,
                    message="Falling back to standard inventories/ endpoint",
                )
                create_data = {k: v for k, v in data.items() if k != "input_inventories"}
                result = await self.client.create_resource(
                    resource_type="inventories",
                    data=create_data,
                    check_exists=True,
                )

            # ALWAYS associate input inventories via the sub-endpoint after
            # creation, regardless of which endpoint was used. The AAP API
            # silently ignores the input_inventories field in the POST body
            # — the only reliable way to link them is via the sub-endpoint.
            if target_input_ids:
                await self._associate_input_inventories(result["id"], target_input_ids)

            # Mark as completed
            self.state.mark_completed(
                resource_type=resource_type,
                source_id=source_id,
                target_id=result["id"],
                target_name=result.get("name"),
            )

            self.stats["imported_count"] += 1

            logger.info(
                "constructed_inventory_imported",
                resource_type=resource_type,
                source_id=source_id,
                target_id=result["id"],
                input_inventory_count=len(target_input_ids),
            )

            return result

        except ConflictError as e:
            logger.warning(
                "constructed_inventory_conflict",
                resource_type=resource_type,
                source_id=source_id,
                error=str(e),
            )
            existing = await self._handle_conflict(resource_type, source_id, data)
            if existing:
                # Also associate input inventories on conflict resolution
                if target_input_ids:
                    try:
                        await self._associate_input_inventories(existing["id"], target_input_ids)
                    except Exception as assoc_err:
                        logger.warning(
                            "constructed_inventory_input_association_failed_on_conflict",
                            target_id=existing["id"],
                            error=str(assoc_err),
                        )
                self.stats["conflict_count"] += 1
                return existing
            else:
                self.stats["error_count"] += 1
                self.state.mark_failed(
                    resource_type=resource_type,
                    source_id=source_id,
                    error_message=f"Conflict: {str(e)}",
                )
                return None

        except Exception as e:
            logger.error(
                "constructed_inventory_import_failed",
                resource_type=resource_type,
                source_id=source_id,
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

    async def _associate_input_inventories(
        self, constructed_inventory_id: int, input_inventory_ids: list[int]
    ) -> None:
        """Associate input inventories with a constructed inventory.

        Uses POST to /inventories/{id}/input_inventories/ to add each
        input inventory to the constructed inventory.

        Args:
            constructed_inventory_id: Target constructed inventory ID
            input_inventory_ids: List of target inventory IDs to associate
        """
        for input_inv_id in input_inventory_ids:
            try:
                endpoint = f"inventories/{constructed_inventory_id}/input_inventories/"
                await self.client.post(endpoint, json_data={"id": input_inv_id})
                logger.info(
                    "input_inventory_associated",
                    constructed_inventory_id=constructed_inventory_id,
                    input_inventory_id=input_inv_id,
                )
            except APIError as e:
                # Ignore "already associated" errors (idempotent)
                if "already" in str(e).lower():
                    logger.info(
                        "input_inventory_already_associated",
                        constructed_inventory_id=constructed_inventory_id,
                        input_inventory_id=input_inv_id,
                    )
                else:
                    logger.error(
                        "input_inventory_association_failed",
                        constructed_inventory_id=constructed_inventory_id,
                        input_inventory_id=input_inv_id,
                        error=str(e),
                    )
                    raise

    async def import_inventories(
        self,
        inventories: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import inventories using two-pass strategy for constructed inventory support.

        Pass 1: Import regular and smart inventories (kind != "constructed")
        Pass 2: Import constructed inventories (kind == "constructed")

        This ordering ensures all input inventories have target ID mappings
        before constructed inventories that reference them are created.

        Args:
            inventories: List of inventory data
            progress_callback: Optional callback for progress updates.
                Called after each inventory with (success_count, failed_count, skipped_count).

        Returns:
            List of created inventory data
        """
        # Split inventories into regular and constructed
        regular_inventories = [inv for inv in inventories if inv.get("kind") != "constructed"]
        constructed_inventories = [inv for inv in inventories if inv.get("kind") == "constructed"]

        logger.info(
            "inventory_import_two_pass_strategy",
            total_inventories=len(inventories),
            regular_count=len(regular_inventories),
            constructed_count=len(constructed_inventories),
            message="Pass 1: regular/smart inventories, Pass 2: constructed inventories",
        )

        all_results = []
        total_success = 0
        total_failed = 0
        total_skipped = 0

        # Pass 1: Import regular/smart inventories
        if regular_inventories:
            logger.info(
                "inventory_import_pass1_starting",
                count=len(regular_inventories),
                message="Importing regular and smart inventories",
            )

            def pass1_progress(success: int, failed: int, skipped: int) -> None:
                nonlocal total_success, total_failed, total_skipped
                if progress_callback:
                    progress_callback(
                        total_success + success,
                        total_failed + failed,
                        total_skipped + skipped,
                    )

            pass1_results = await self._import_parallel(
                "inventories", regular_inventories, progress_callback=pass1_progress
            )

            pass1_success = len([r for r in pass1_results if r and not r.get("_skipped")])
            pass1_skipped = len([r for r in pass1_results if r and r.get("_skipped")])
            pass1_failed = len(regular_inventories) - pass1_success - pass1_skipped

            total_success += pass1_success
            total_failed += pass1_failed
            total_skipped += pass1_skipped
            all_results.extend(pass1_results)

            logger.info(
                "inventory_import_pass1_completed",
                success=pass1_success,
                failed=pass1_failed,
                skipped=pass1_skipped,
            )

        # Pass 2: Import constructed inventories (sequentially to handle dependencies)
        if constructed_inventories:
            logger.info(
                "inventory_import_pass2_starting",
                count=len(constructed_inventories),
                message="Importing constructed inventories with input_inventories",
            )

            def pass2_progress(success: int, failed: int, skipped: int) -> None:
                nonlocal total_success, total_failed, total_skipped
                if progress_callback:
                    progress_callback(
                        total_success + success,
                        total_failed + failed,
                        total_skipped + skipped,
                    )

            pass2_results = await self._import_parallel(
                "inventories", constructed_inventories, progress_callback=pass2_progress
            )

            pass2_success = len([r for r in pass2_results if r and not r.get("_skipped")])
            pass2_skipped = len([r for r in pass2_results if r and r.get("_skipped")])
            pass2_failed = len(constructed_inventories) - pass2_success - pass2_skipped

            total_success += pass2_success
            total_failed += pass2_failed
            total_skipped += pass2_skipped
            all_results.extend(pass2_results)

            logger.info(
                "inventory_import_pass2_completed",
                success=pass2_success,
                failed=pass2_failed,
                skipped=pass2_skipped,
            )

        logger.info(
            "inventory_import_two_pass_completed",
            total_success=total_success,
            total_failed=total_failed,
            total_skipped=total_skipped,
        )

        return all_results
