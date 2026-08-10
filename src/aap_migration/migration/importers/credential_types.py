from collections.abc import Callable
from typing import Any

from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class CredentialTypeImporter(ResourceImporter):
    """Importer for credential type resources.

    Credential types are pre-created in the target environment before migration.
    This importer PATCHes existing resources instead of POSTing new ones.
    """

    DEPENDENCIES: dict[str, str] = {
        "organization": "organizations",
    }

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Import credential_type by PATCHing existing resource in target.

        Credential types are pre-created in the target environment. This method
        finds the existing resource by name and PATCHes it with organization
        and description from the source.

        Args:
            resource_type: Type of resource being imported
            source_id: Source resource ID (from source AAP)
            data: Transformed resource data
            resolve_dependencies: Whether to resolve foreign key dependencies

        Returns:
            Patched resource data or None if skipped/failed
        """
        # Check if already imported
        if self.state.is_migrated(resource_type, source_id):
            logger.debug(
                "resource_already_imported",
                resource_type=resource_type,
                source_id=source_id,
            )
            self.stats["skipped_count"] += 1
            return None

        name = data.get("name")
        if not name:
            logger.error("credential_type_missing_name", source_id=source_id)
            self.stats["error_count"] += 1
            return None

        # Mark as in progress
        self.state.mark_in_progress(
            resource_type=resource_type,
            source_id=source_id,
            source_name=name,
            phase="import",
        )

        try:
            # Find existing credential_type in target by name
            results = await self.client.get("credential_types/", params={"name": name})
            resources = results.get("results", [])

            if resources:
                # Found - PATCH existing
                target_id = resources[0]["id"]
                is_managed = resources[0].get("managed", False)

                # Skip PATCH for managed (built-in) credential types
                if is_managed:
                    logger.info(
                        "credential_type_managed_skip_patch",
                        name=name,
                        source_id=source_id,
                        target_id=target_id,
                        message="Skipping PATCH for managed credential type - saving mapping only",
                    )
                    self.state.save_id_mapping(
                        resource_type=resource_type,
                        source_id=source_id,
                        target_id=target_id,
                        source_name=name,
                        target_name=name,
                    )
                    self.state.mark_completed(
                        resource_type=resource_type,
                        source_id=source_id,
                        target_id=target_id,
                        target_name=name,
                    )
                    self.stats["skipped_count"] += 1
                    # Return skipped signal
                    return {
                        "id": target_id,
                        "name": name,
                        "_skipped": True,
                        "_skip_reason": (
                            f"Managed credential type on target (id {target_id}) — "
                            "mapped only, not patched"
                        ),
                    }

                # Resolve dependencies (organization)
                if resolve_dependencies:
                    data = await self._resolve_dependencies(resource_type, data)

                # Build PATCH payload (organization, description only)
                patch_data = {}
                if data.get("organization"):
                    patch_data["organization"] = data["organization"]
                if data.get("description"):
                    patch_data["description"] = data["description"]

                # PATCH the credential_type if there's data to update
                if patch_data:
                    await self.client.update_resource("credential_types", target_id, patch_data)
                    logger.info(
                        "credential_type_patched",
                        name=name,
                        source_id=source_id,
                        target_id=target_id,
                        patched_fields=list(patch_data.keys()),
                    )
                else:
                    logger.info(
                        "credential_type_mapped_no_patch",
                        name=name,
                        source_id=source_id,
                        target_id=target_id,
                        message="No fields to patch - mapping only",
                    )

                result = {"id": target_id, "name": name, "_patched": bool(patch_data)}

            else:
                # Not found - CREATE new

                # Skip creation of external credential types (they must exist in target)
                if data.get("kind") == "external":
                    logger.warning(
                        "skipping_external_credential_type_creation",
                        name=name,
                        source_id=source_id,
                        message="External credential type not found in target - skipping creation per policy",
                    )
                    self.stats["skipped_count"] += 1
                    return None

                logger.info(
                    "credential_type_creating",
                    name=name,
                    source_id=source_id,
                    message="Creating new credential type",
                )

                # Resolve dependencies (organization)
                if resolve_dependencies:
                    data = await self._resolve_dependencies(resource_type, data)

                # Create resource
                result = await self.client.create_resource(
                    resource_type="credential_types",
                    data=data,
                    check_exists=False,
                )
                target_id = result["id"]
                logger.info(
                    "credential_type_created",
                    name=name,
                    source_id=source_id,
                    target_id=target_id,
                )

            # Save mapping
            self.state.save_id_mapping(
                resource_type=resource_type,
                source_id=source_id,
                target_id=target_id,
                source_name=name,
                target_name=name,
            )
            self.state.mark_completed(
                resource_type=resource_type,
                source_id=source_id,
                target_id=target_id,
                target_name=name,
            )
            self.stats["imported_count"] += 1

            return result

        except Exception as e:
            logger.error(
                "credential_type_import_failed",
                source_id=source_id,
                name=name,
                error=str(e),
            )
            self.stats["error_count"] += 1
            self.import_errors.append(
                {
                    "resource_type": resource_type,
                    "source_id": source_id,
                    "name": name,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
            )
            return None

    async def import_credential_types(
        self,
        credential_types: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple credential types by PATCHing pre-existing resources.

        Credential types are pre-created in the target environment before migration.
        This method finds each credential type by name and PATCHes it with
        organization and description from the source.

        Args:
            credential_types: List of credential type data
            progress_callback: Optional callback for progress updates.
                Called after each credential type with (success_count, failed_count).

        Returns:
            List of patched credential type data
        """
        logger.info(
            "credential_types_import_starting",
            total_count=len(credential_types),
            names=[ct.get("name") for ct in credential_types],
            message="PATCHing pre-created credential types in target",
        )

        # All credential types go through the same PATCH flow via import_resource()
        results = await self._import_parallel(
            "credential_types", credential_types, progress_callback
        )

        logger.info(
            "credential_types_import_completed",
            total_input=len(credential_types),
            patched_count=len(results),
            skipped_or_failed=len(credential_types) - len(results),
        )

        return results
