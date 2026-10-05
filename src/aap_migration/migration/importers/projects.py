"""Domain resource importers (split from migration.importer; re-exported)."""

import asyncio
from collections.abc import Callable
from typing import Any, cast

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class CredentialImporter(ResourceImporter):
    """Importer for credential resources.

    Credentials are pre-created in the target environment before migration.
    This importer PATCHes existing resources instead of POSTing new ones.
    """

    DEPENDENCIES = {
        "organization": "organizations",
        "credential_type": "credential_types",
        "user": "users",
        "team": "teams",
    }

    # Built-in credential type IDs (managed by AAP, consistent across versions)
    # Built-in types are IDs 1-27 in AAP 2.3, 2.4, 2.5, and 2.6
    # Custom types start at ID 28+
    #
    # NOTE: This assumption should be verified for your specific AAP versions.
    # If your source or target AAP has different built-in credential type IDs,
    # adjust this value accordingly. You can verify by checking:
    #   GET /api/v2/credential_types/?managed=true
    # on both source and target AAP instances.
    BUILTIN_CREDENTIAL_TYPE_MAX_ID = 27

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Import credential by PATCHing existing resource in target.

        Credentials are pre-created in the target environment. This method
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

        # Mark as in progress (creates MigrationProgress record for mark_completed)
        self.state.mark_in_progress(
            resource_type=resource_type,
            source_id=source_id,
            source_name=name or "unknown",
            phase="import",
        )

        if not name:
            logger.error("credential_missing_name", source_id=source_id)
            self.stats["error_count"] += 1
            return None

        # Work on a copy so caller-owned dicts are never mutated.
        data = dict(data)
        # Clean up transformer markers
        data.pop("_temp_credential_values", None)
        data.pop("_encrypted_fields", None)
        data.pop("_needs_vault_lookup", None)

        # Resolve dependencies BEFORE lookup to get target org/credential_type IDs
        # This ensures we can search by the complete composite key
        if resolve_dependencies:
            data = await self._resolve_dependencies(resource_type, data)

        try:
            # Build query params for exact match: (name, organization, credential_type)
            # Credentials are unique by this composite key in AAP
            query_params = {"name": name}

            # Add organization to query if present
            # Note: Some credentials may have organization=null (system/global credentials)
            if "organization" in data and data["organization"] is not None:
                query_params["organization"] = data["organization"]

            # Add credential_type to query if present
            if "credential_type" in data and data["credential_type"] is not None:
                query_params["credential_type"] = data["credential_type"]

            # Find existing credential in target by composite key
            logger.debug(
                "credential_lookup",
                name=name,
                source_id=source_id,
                query_params=query_params,
                message="Looking up credential by composite key (name, org, type)",
            )
            results = await self.client.get("credentials/", params=query_params)
            resources = results.get("results", [])

            if resources:
                # Credential exists - PATCH it
                target_id = resources[0]["id"]
                is_managed = resources[0].get("managed", False)

                logger.info(
                    "credential_found_in_target",
                    name=name,
                    source_id=source_id,
                    target_id=target_id,
                    organization=data.get("organization"),
                    credential_type=data.get("credential_type"),
                    message="Found existing credential with matching name/org/type",
                )

                # Skip PATCH for managed (built-in) credentials - AAP doesn't allow modifications
                if is_managed:
                    logger.info(
                        "credential_managed_skip_patch",
                        name=name,
                        source_id=source_id,
                        target_id=target_id,
                        message="Skipping PATCH for managed credential - saving mapping only",
                    )
                    # Save mapping without patching
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
                    return {"id": target_id, "name": name, "_skipped": True}

                # Build PATCH payload (organization, description only)
                # Note: Dependencies already resolved above before lookup
                patch_data = {}
                if data.get("organization"):
                    patch_data["organization"] = data["organization"]
                if data.get("description"):
                    patch_data["description"] = data["description"]

                # PATCH the credential
                if patch_data:
                    await self.client.update_resource("credentials", target_id, patch_data)
                    logger.info(
                        "credential_patched",
                        name=name,
                        source_id=source_id,
                        target_id=target_id,
                        patched_fields=list(patch_data.keys()),
                    )
                else:
                    logger.info(
                        "credential_mapped_no_patch",
                        name=name,
                        source_id=source_id,
                        target_id=target_id,
                        message="No fields to patch - mapping only",
                    )

                result = {"id": target_id, "name": name, "_patched": bool(patch_data)}

            else:
                # Credential does not exist - CREATE it
                logger.info(
                    "credential_creating",
                    name=name,
                    source_id=source_id,
                    organization=data.get("organization"),
                    credential_type=data.get("credential_type"),
                    message="Creating new credential - no match found for name/org/type composite key",
                )

                # Dependencies already resolved above before lookup
                # Create resource
                result = await self.client.create_resource(
                    resource_type="credentials",
                    data=data,
                    check_exists=False,  # We already checked with composite key
                )

                target_id = result["id"]
                logger.info(
                    "credential_created",
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
                "credential_import_failed",
                source_id=source_id,
                name=name,
                error=str(e),
            )
            self.stats["error_count"] += 1

            # Mark as failed in database to prevent stuck "in_progress" state
            self.state.mark_failed(
                resource_type=resource_type,
                source_id=source_id,
                error_message=f"{type(e).__name__}: {str(e)}",
            )

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

    async def _resolve_dependencies(
        self, resource_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Override to handle built-in credential types.

        Built-in credential types (IDs 1-27) are managed by AAP and not exported
        because they already exist in both AAP 2.3 and AAP 2.6. We assume they
        have consistent IDs between versions.

        Custom credential types (IDs 28+) use normal ID mapping resolution.

        Args:
            resource_type: The resource type being imported
            data: The resource data with source IDs

        Returns:
            Resource data with resolved target IDs
        """
        resolved = dict(data)

        # Handle credential_type field specially
        if "credential_type" in data and data["credential_type"]:
            source_id = data["credential_type"]
            target_id = self.state.get_mapped_id("credential_types", source_id)

            if target_id:
                # Custom credential type - use mapping
                resolved["credential_type"] = target_id
                logger.debug(
                    "resolved_custom_credential_type",
                    credential_name=data.get("name"),
                    source_id=source_id,
                    target_id=target_id,
                )
            else:
                # No mapping found
                if source_id <= self.BUILTIN_CREDENTIAL_TYPE_MAX_ID:
                    # Built-in credential type - assume same ID in AAP 2.6
                    logger.debug(
                        "using_builtin_credential_type",
                        credential_name=data.get("name"),
                        credential_type_id=source_id,
                        message="Assuming consistent ID for built-in credential type",
                    )
                    # Keep original ID (assumption: built-in types have same IDs)
                    resolved["credential_type"] = source_id
                else:
                    # Custom type (ID > 27) but no mapping = ERROR
                    logger.error(
                        "missing_custom_credential_type_mapping",
                        credential_name=data.get("name"),
                        source_id=source_id,
                        message="Custom credential type not found in ID mappings",
                    )
                    # Remove field to allow partial import (existing behavior)
                    resolved.pop("credential_type", None)

        # Resolve other dependencies (organization, user, team) using base logic
        for field, dep_resource_type in self.DEPENDENCIES.items():
            # Skip credential_type - already handled above
            if field == "credential_type":
                continue

            if field in data and data[field]:
                source_id = data[field]
                target_id = self.state.get_mapped_id(dep_resource_type, source_id)
                if target_id:
                    resolved[field] = target_id
                    logger.debug(
                        f"resolved_{field}_dependency",
                        credential_name=data.get("name"),
                        source_id=source_id,
                        target_id=target_id,
                    )
                else:
                    logger.warning(
                        "unresolved_dependency",
                        resource_name=data.get("name"),
                        field=field,
                        source_id=source_id,
                        dep_resource_type=dep_resource_type,
                    )
                    # Remove field to allow partial import
                    resolved.pop(field, None)

        return resolved

    async def import_credentials(
        self,
        credentials: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple credentials by PATCHing pre-existing resources.

        Credentials are pre-created in the target environment before migration.
        This method finds each credential by name and PATCHes it with
        organization and description from the source.

        Note: Encrypted fields (secrets) are NOT patched - they are already
        set during the external credential creation process.

        Args:
            credentials: List of credential data
            progress_callback: Optional callback for progress updates.
                Called after each credential with (success_count, failed_count).

        Returns:
            List of patched credential data
        """
        logger.info(
            "credentials_import_starting",
            total_count=len(credentials),
            names=[c.get("name") for c in credentials],
            message="PATCHing pre-created credentials in target",
        )

        # Clean up transformer marker fields before import (non-mutating:
        # build copies so a second pass still sees the original batch).
        clean_credentials = [
            {
                k: v
                for k, v in c.items()
                if k not in ("_encrypted_fields", "_temp_credential_values")
            }
            for c in credentials
        ]

        # All credentials go through the same PATCH flow via import_resource()
        results = await self._import_parallel("credentials", clean_credentials, progress_callback)

        logger.info(
            "credentials_import_completed",
            total_input=len(credentials),
            patched_count=len(results),
            skipped_or_failed=len(credentials) - len(results),
        )

        return results

    def _detect_encrypted_fields(self, credential: dict[str, Any]) -> list[str]:
        """Detect fields with $encrypted$ values.

        Checks both:
        1. The _encrypted_fields marker added by transformer
        2. Current inputs dict for any remaining $encrypted$ values

        Args:
            credential: Credential data

        Returns:
            List of field names that have encrypted values
        """
        encrypted_fields = []

        # First check the transformer marker (transformer already removed $encrypted$ from inputs)
        if "_encrypted_fields" in credential:
            encrypted_fields.extend(credential["_encrypted_fields"])

        # Also check current inputs for any $encrypted$ values that weren't cleaned
        if "inputs" in credential and isinstance(credential["inputs"], dict):
            for key, value in credential["inputs"].items():
                if value == "$encrypted$" and key not in encrypted_fields:
                    encrypted_fields.append(key)

        return encrypted_fields


class ProjectImporter(ResourceImporter):
    """Importer for project resources."""

    DEPENDENCIES = {
        "organization": "organizations",
        "credential": "credentials",
        "default_environment": "execution_environments",
    }

    async def import_projects(
        self,
        projects: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple projects concurrently with live progress updates.

        Args:
            projects: List of project data
            progress_callback: Optional callback for progress updates.
                Called after each project with (success_count, failed_count).

        Returns:
            List of created project data
        """
        # Extract schedules before import (non-mutating: keep caller dicts
        # intact so a second pass still sees schedules/_source_id).
        projects_with_schedules = []
        for project in projects:
            schedules = project.get("schedules", None)
            if schedules:
                source_id = project.get("_source_id", project.get("id"))
                projects_with_schedules.append(
                    {
                        "source_project_id": source_id,
                        "schedules": schedules,
                    }
                )

        # Import projects without the nested schedules key.
        clean_projects = [{k: v for k, v in p.items() if k != "schedules"} for p in projects]
        results = await self._import_parallel("projects", clean_projects, progress_callback)

        # Import schedules for successfully imported projects
        if projects_with_schedules:
            logger.info(
                "importing_project_schedules",
                total_projects_with_schedules=len(projects_with_schedules),
            )

            for schedule_data in projects_with_schedules:
                source_project_id = schedule_data["source_project_id"]
                schedules = schedule_data["schedules"]

                # Get the target project ID from the state mapping
                target_project_id = self.state.get_mapped_id("projects", source_project_id)
                if not target_project_id:
                    logger.warning(
                        "project_not_found_for_schedule",
                        source_project_id=source_project_id,
                    )
                    continue

                # Get project name for logging
                project_result = next(
                    (p for p in results if p.get("id") == target_project_id), None
                )
                project_name = (
                    project_result.get("name", "unknown") if project_result else "unknown"
                )

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
                            f"projects/{target_project_id}/schedules/",
                            json_data=schedule_to_import,
                        )
                        logger.info(
                            "project_schedule_imported",
                            project_id=target_project_id,
                            project_name=project_name,
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
                                    "project_schedule_tracked",
                                    source_id=source_schedule_id,
                                    target_id=result.get("id"),
                                    schedule_name=schedule_name,
                                )
                            except Exception as tracking_error:
                                # Don't fail schedule import if tracking fails
                                logger.warning(
                                    "project_schedule_tracking_failed",
                                    source_id=source_schedule_id,
                                    target_id=result.get("id"),
                                    schedule_name=schedule_name,
                                    error=str(tracking_error),
                                )
                    except Exception as e:
                        logger.error(
                            "project_schedule_import_failed",
                            project_id=target_project_id,
                            project_name=project_name,
                            schedule_name=schedule_name,
                            error=str(e),
                        )

        return results


async def wait_for_project_sync(
    client: "AAPTargetClient",
    project_ids: list[int],
    timeout: int = 600,
    poll_interval: int = 10,
    progress_callback: Callable[[int, int, int], None] | None = None,
) -> tuple[int, int, list[int]]:
    """Wait for projects to complete SCM sync after import.

    After projects are imported to AAP, they automatically trigger an SCM sync.
    Job templates cannot be created until the sync completes because the playbooks
    don't exist yet. This function polls project status and waits for sync completion.

    Args:
        client: Target AAP client
        project_ids: List of target project IDs to wait for
        timeout: Maximum time to wait in seconds (default 600 = 10 minutes)
        poll_interval: Time between status checks in seconds (default 10)
        progress_callback: Optional callback for progress updates (completed, total)

    Returns:
        Tuple of (synced_count, failed_count, list_of_failed_project_ids)
    """
    import time

    if not project_ids:
        return (0, 0, [])

    logger.info(
        "waiting_for_project_sync",
        project_count=len(project_ids),
        timeout=timeout,
        poll_interval=poll_interval,
    )

    start_time = time.time()
    synced: set[int] = set()
    failed: set[int] = set()

    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout:
            # Timeout - remaining projects count as failed
            remaining = set(project_ids) - synced - failed
            logger.warning(
                "project_sync_timeout",
                synced=len(synced),
                failed=len(failed),
                timed_out=len(remaining),
                elapsed_seconds=int(elapsed),
            )
            return (len(synced), len(failed) + len(remaining), list(failed | remaining))

        # Check status of remaining projects
        pending = set(project_ids) - synced - failed

        for project_id in list(pending):
            try:
                project = await client.get(f"projects/{project_id}/")
                status = project.get("status", "unknown")
                scm_type = project.get("scm_type", "")

                # Manual projects (no SCM) - no sync needed
                if not scm_type:
                    synced.add(project_id)
                    logger.debug(
                        "project_no_scm_skip",
                        project_id=project_id,
                        name=project.get("name"),
                    )
                    continue

                # Project synced successfully
                if status == "successful":
                    synced.add(project_id)
                    logger.debug(
                        "project_sync_complete",
                        project_id=project_id,
                        name=project.get("name"),
                    )
                # Project sync failed
                elif status in ("failed", "error", "canceled"):
                    failed.add(project_id)
                    logger.warning(
                        "project_sync_failed",
                        project_id=project_id,
                        name=project.get("name"),
                        status=status,
                    )
                # Still syncing (pending, waiting, running) - continue waiting

            except Exception as e:
                logger.warning(
                    "project_status_check_error",
                    project_id=project_id,
                    error=str(e),
                )

        # Update progress
        if progress_callback:
            progress_callback(len(synced), len(failed), 0)

        # All projects done
        if len(synced) + len(failed) >= len(project_ids):
            logger.info(
                "project_sync_wait_complete",
                synced=len(synced),
                failed=len(failed),
                elapsed_seconds=int(time.time() - start_time),
            )
            return (len(synced), len(failed), list(failed))

        await asyncio.sleep(poll_interval)
