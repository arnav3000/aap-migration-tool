"""Resource importers for importing data to AAP 2.6.

Base importer class shared by every domain module in this package.
"""

import asyncio
from collections.abc import Callable
from typing import Any

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.client.exceptions import APIError, ConflictError
from aap_migration.config import PerformanceConfig
from aap_migration.migration.importers._base_helpers import BaseLookupMixin
from aap_migration.migration.importers._registry import (
    ORGANIZATION_REQUIRED_RESOURCES,
    ORGANIZATION_SCOPED_RESOURCES,
)
from aap_migration.migration.state import MigrationState
from aap_migration.resources import PARENT_SCOPED_RESOURCES
from aap_migration.utils.idempotency import compare_resources
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class ResourceImporter(BaseLookupMixin):
    """Base class for importing resources to AAP 2.6.

    Handles dependency resolution, conflict detection, and state tracking.
    """

    # Dependency mapping: field_name -> resource_type
    DEPENDENCIES: dict[str, str] = {}

    # Identifier field used for uniqueness checks (override in subclasses if different)
    IDENTIFIER_FIELD = "name"

    def _get_dependencies(self, resource_type: str) -> dict[str, str]:
        """Get dependency mapping for resource type.

        Args:
            resource_type: Type of resource (legacy parameter kept for
                downstream overrides that branch per type; the base
                implementation ignores it and returns class DEPENDENCIES).

        Returns:
            Dictionary mapping field names to resource types
        """
        # Use class-level DEPENDENCIES or return empty dict.
        # Kept as a hook (not a direct DEPENDENCIES read at call sites) so
        # downstream subclasses overriding it for dynamic per-type mappings
        # keep working, as they did against migration.importer.
        return self.DEPENDENCIES

    def __init__(
        self,
        client: AAPTargetClient,
        state: MigrationState,
        performance_config: PerformanceConfig,
        resource_mappings: dict[str, dict[str, str]] | None = None,
    ):
        """Initialize resource importer.

        Args:
            client: AAP target client instance
            state: Migration state manager
            performance_config: Performance configuration
            resource_mappings: Optional resource name mappings from config/mappings.yaml
        """
        self.client = client
        self.state = state
        self.performance_config = performance_config
        self.resource_mappings = resource_mappings or {}
        self.stats = {
            "imported_count": 0,
            "error_count": 0,
            "conflict_count": 0,
            "skipped_count": 0,
        }
        # Track issues for reporting
        self.unresolved_dependencies: list[dict[str, Any]] = []
        self.import_errors: list[dict[str, Any]] = []

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Import a single resource to AAP 2.6.

        Args:
            resource_type: Type of resource being imported
            source_id: Source resource ID (from source AAP)
            data: Transformed resource data
            resolve_dependencies: Whether to resolve foreign key dependencies

        Returns:
            Created resource data or None if skipped
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

        # Mark as in progress
        self.state.mark_in_progress(
            resource_type=resource_type,
            source_id=source_id,
            source_name=data.get(self.IDENTIFIER_FIELD, data.get("name", "unknown")),
            phase="import",
        )

        try:
            # Resolve dependencies
            if resolve_dependencies:
                data = await self._resolve_dependencies(resource_type, data)

            # VALIDATION: Check required fields
            # Some resources MUST have an organization to be created in AAP
            if resource_type in ORGANIZATION_REQUIRED_RESOURCES:
                organization_id = data.get("organization")
                if organization_id is None:
                    error_msg = (
                        f"Missing required field 'organization' for {resource_type}. "
                        f"Resource '{data.get('name', 'unknown')}' (source ID {source_id}) "
                        f"cannot be created without an organization. This indicates invalid "
                        f"data in source AAP that needs manual correction."
                    )
                    logger.error(
                        "validation_failed_missing_organization",
                        resource_type=resource_type,
                        source_id=source_id,
                        name=data.get("name"),
                        error=error_msg,
                    )
                    self.stats["error_count"] += 1
                    self.state.mark_failed(
                        resource_type=resource_type,
                        source_id=source_id,
                        error_message=f"Validation failed: {error_msg}",
                    )
                    self.import_errors.append(
                        {
                            "resource_type": resource_type,
                            "source_id": source_id,
                            "name": data.get("name", "unknown"),
                            "error": error_msg,
                            "error_type": "ValidationError",
                        }
                    )
                    return None

            # Remove None/null values from data before API call
            # AAP 2.6 API requires null-valued fields to be absent, not sent as null
            # EXCEPTION: Preserve None for credential ownership fields (organization/user/team)
            # Credentials require at least one ownership field, even if None
            ownership_fields = {"user", "team"}
            data = {k: v for k, v in data.items() if v is not None or k in ownership_fields}

            # DUPLICATE DETECTION: Check if resource already exists in target AAP
            # This prevents creating duplicates when database mapping is missing
            resource_name = data.get("name")
            if resource_name:
                try:
                    # For organization-scoped resources, check duplicates within same org only
                    # This prevents mapping resources with same name in different orgs to single target
                    organization_id = None
                    parent_id = None
                    parent_field = None
                    skip_duplicate_check = False

                    if resource_type in ORGANIZATION_SCOPED_RESOURCES:
                        organization_id = data.get("organization")

                        # Skip duplicate detection if organization is None
                        # Passing None would search globally, incorrectly matching resources from other orgs
                        # Note: Resources requiring org were already validated above and failed if org=None
                        # This handles resources that CAN be global (like credentials, execution_environments)
                        if organization_id is None:
                            skip_duplicate_check = True
                            logger.debug(
                                "skipping_duplicate_detection_no_org",
                                resource_type=resource_type,
                                source_id=source_id,
                                name=resource_name,
                                reason="organization_is_none",
                            )

                    # For parent-scoped resources, check duplicates within same parent only
                    # This prevents mapping resources with same name in different parents to single target
                    elif resource_type in PARENT_SCOPED_RESOURCES:
                        parent_field = PARENT_SCOPED_RESOURCES[resource_type]
                        parent_id = data.get(parent_field)

                        # Skip duplicate detection if parent is None
                        # Passing None would search globally, incorrectly matching resources from other parents
                        if parent_id is None:
                            skip_duplicate_check = True
                            logger.debug(
                                "skipping_duplicate_detection_no_parent",
                                resource_type=resource_type,
                                source_id=source_id,
                                name=resource_name,
                                parent_field=parent_field,
                                reason="parent_is_none",
                            )

                    if skip_duplicate_check:
                        # Skip duplicate check - will attempt creation
                        # If duplicate exists, API will return 409/400 and we handle it below
                        existing = None
                    else:
                        existing = await self.client.find_resource_by_name(
                            resource_type,
                            resource_name,
                            organization_id=organization_id,
                            parent_id=parent_id,
                            parent_field=parent_field,
                        )
                    if existing:
                        logger.warning(
                            "resource_exists_but_not_mapped",
                            resource_type=resource_type,
                            source_id=source_id,
                            target_id=existing["id"],
                            name=resource_name,
                            organization_id=organization_id,
                            parent_id=parent_id,
                            parent_field=parent_field,
                            action="marking_as_skipped_duplicate",
                        )
                        # Mark as skipped with target_id (duplicate detection)
                        self.state.mark_skipped(
                            resource_type=resource_type,
                            source_id=source_id,
                            reason=f"Duplicate exists in target (name: {resource_name}, target_id: {existing['id']})",
                            target_id=existing["id"],
                            target_name=existing.get("name"),
                            source_name=resource_name,
                        )
                        self.stats["skipped_count"] += 1
                        return existing
                except Exception as e:
                    # If lookup fails, continue with normal create (don't break import)
                    logger.debug(
                        "duplicate_detection_failed",
                        resource_type=resource_type,
                        error=str(e),
                        action="continuing_with_create",
                    )

            # Create resource
            result = await self.client.create_resource(
                resource_type=resource_type,
                data=data,
                check_exists=True,
            )

            # Mark as completed
            self.state.mark_completed(
                resource_type=resource_type,
                source_id=source_id,
                target_id=result["id"],
                target_name=result.get(self.IDENTIFIER_FIELD) or result.get("name"),
            )

            self.stats["imported_count"] += 1

            logger.info(
                "resource_imported",
                resource_type=resource_type,
                source_id=source_id,
                target_id=result["id"],
            )

            return result

        except ConflictError as e:
            # Handle conflict - resource already exists (409)
            logger.warning(
                "resource_conflict",
                resource_type=resource_type,
                source_id=source_id,
                error=str(e),
            )

            # Try to resolve conflict
            existing = await self._handle_conflict(resource_type, source_id, data)
            if existing:
                self.stats["conflict_count"] += 1
                return existing
            else:
                self.stats["error_count"] += 1
                self.state.mark_failed(
                    resource_type=resource_type,
                    source_id=source_id,
                    error_message=f"Conflict ({type(e).__name__}): {str(e)}",
                )
                return None

        except APIError as e:
            # Check if it's an "already exists" error (400 with specific message)
            error_str = str(e).lower()
            is_already_exists = "already exists" in error_str or (
                e.response
                and any(
                    "already exists" in str(v).lower()
                    for v in (e.response.values() if isinstance(e.response, dict) else [])
                )
            )

            if is_already_exists:
                # Treat as conflict - resource already exists (400 with "already exists")
                logger.warning(
                    "resource_already_exists",
                    resource_type=resource_type,
                    source_id=source_id,
                    error=str(e),
                )

                # Try to resolve conflict
                existing = await self._handle_conflict(resource_type, source_id, data)
                if existing:
                    self.stats["conflict_count"] += 1
                    return existing
                else:
                    self.stats["error_count"] += 1
                    self.state.mark_failed(
                        resource_type=resource_type,
                        source_id=source_id,
                        error_message=f"Already exists ({type(e).__name__}): {str(e)}",
                    )
                    return None
            else:
                # Not an "already exists" error - enrich error message with source context
                enriched_error = self._enrich_api_error_message(e, resource_type, data)

                self.stats["error_count"] += 1
                self.state.mark_failed(
                    resource_type=resource_type,
                    source_id=source_id,
                    error_message=enriched_error,
                )
                return None

        except Exception as e:
            logger.error(
                "resource_import_failed",
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

            return None

    def _enrich_api_error_message(
        self, error: APIError, resource_type: str, data: dict[str, Any]
    ) -> str:
        """Enrich API error message with source dependency context.

        When an API error occurs due to missing/invalid dependencies, this method
        enhances the error message to show source resource names and IDs instead
        of just target IDs, making it easier for users to understand what failed.

        Args:
            error: The APIError exception
            resource_type: Type of resource being imported
            data: Resource data with source information

        Returns:
            Enhanced error message with source context
        """
        base_error = f"API error ({type(error).__name__}): {str(error)}"

        # Only enrich if we have a response dict with field-level errors
        if not error.response or not isinstance(error.response, dict):
            return base_error

        # Dependencies for this resource type (may be empty for some importers)
        dependencies = self._get_dependencies(resource_type)

        # Parse error response to find dependency-related failures
        enriched_parts = []
        for field, field_errors in error.response.items():
            # Convert field_errors to list if it's not already
            error_list = field_errors if isinstance(field_errors, list) else [field_errors]

            # Look for "Invalid pk" or "does not exist" errors (dependency failures)
            field_enriched = False
            for field_error in error_list:
                error_str = str(field_error)
                if "invalid pk" in error_str.lower() or "does not exist" in error_str.lower():
                    # Extract source dependency info from original data
                    dep_source_id = data.get(field)

                    if dep_source_id:
                        # Try to infer resource type from field name or use dependencies dict
                        dep_resource_type = dependencies.get(field) if dependencies else None

                        # If not in dependencies, try to infer from field name
                        if not dep_resource_type:
                            # Common field patterns: inventory, project, organization, credential, etc.
                            dep_resource_type = self._infer_resource_type_from_field(field)

                        # Try to get the source dependency name from database
                        dep_name = (
                            self._get_dependency_name(dep_resource_type, dep_source_id)
                            if dep_resource_type
                            else None
                        )

                        if dep_name:
                            resource_kind = (
                                dep_resource_type
                                if isinstance(dep_resource_type, str)
                                else str(dep_resource_type)
                            )
                            enriched_parts.append(
                                f"{field}: {resource_kind.rstrip('s').replace('_', ' ').title()} "
                                f"'{dep_name}' (source ID: {dep_source_id}) does not exist in target AAP"
                            )
                            field_enriched = True
                        elif dep_resource_type:
                            enriched_parts.append(
                                f"{field}: {dep_resource_type.rstrip('s').replace('_', ' ').title()} "
                                f"with source ID {dep_source_id} does not exist in target AAP"
                            )
                            field_enriched = True

            # If we didn't enrich this field, include the original error
            if not field_enriched:
                enriched_parts.append(f"{field}: {field_errors}")

        # If we enriched any dependency errors, use the enriched message
        if enriched_parts:
            return f"API error: {'; '.join(enriched_parts)}"

        return base_error

    def _add_notification_warnings(
        self, resource_type: str, warnings_by_source_id: dict[int, list[str]]
    ) -> None:
        """Add notification association warnings to resource records in database.

        Delegates to :meth:`MigrationState.append_notification_warnings`,
        the single home for this update (same completed-only, append-only
        semantics, plus state-lock discipline).
        """
        try:
            self.state.append_notification_warnings(resource_type, warnings_by_source_id)
        except Exception as e:
            logger.error(
                "failed_to_add_notification_warnings",
                resource_type=resource_type,
                error=str(e),
            )

    async def _resolve_dependencies(
        self, resource_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve foreign key dependencies using ID mappings.

        Args:
            resource_type: Type of resource
            data: Resource data

        Returns:
            Data with resolved dependencies
        """
        resolved = dict(data)
        dependencies = self._get_dependencies(resource_type)
        resource_source_id = data.get("_source_id") or data.get("id")

        logger.debug(
            "dependency_resolution_start",
            resource_type=resource_type,
            source_id=resource_source_id,
            source_name=data.get("name"),
            dependencies=dependencies,
            data_fields=list(data.keys()),
        )

        for field, dep_resource_type in dependencies.items():
            if field in data and data[field]:
                dep_source_id = data[field]

                logger.debug(
                    "resolving_dependency_field",
                    resource_type=resource_type,
                    source_id=resource_source_id,
                    field=field,
                    dep_source_id=dep_source_id,
                    dep_resource_type=dep_resource_type,
                )

                # Get mapped target ID
                target_id = self.state.get_mapped_id(dep_resource_type, dep_source_id)

                logger.debug(
                    "dependency_mapping_lookup",
                    resource_type=resource_type,
                    source_id=resource_source_id,
                    field=field,
                    dep_resource_type=dep_resource_type,
                    dep_source_id=dep_source_id,
                    target_id=target_id,
                    found=target_id is not None,
                )

                if target_id:
                    resolved[field] = target_id
                    logger.debug(
                        "dependency_resolved",
                        resource_type=resource_type,
                        source_id=resource_source_id,
                        field=field,
                        dep_source_id=dep_source_id,
                        target_id=target_id,
                    )
                else:
                    # Track unresolved dependency for reporting
                    self.unresolved_dependencies.append(
                        {
                            "resource_type": resource_type,
                            "resource_name": data.get("name", "unknown"),
                            "source_id": resource_source_id,
                            "dependency_field": field,
                            "dependency_type": dep_resource_type,
                            "missing_source_id": dep_source_id,
                            "error": f"No mapping found for {dep_resource_type} ID {dep_source_id}",
                        }
                    )

                    logger.warning(
                        "unresolved_dependency",
                        resource_type=resource_type,
                        source_id=resource_source_id,
                        source_name=data.get("name"),
                        field=field,
                        dep_source_id=dep_source_id,
                        dep_resource_type=dep_resource_type,
                    )

                    # Remove the field to allow partial import
                    # (resource will be created without this dependency)
                    resolved.pop(field, None)

        return resolved

    async def _handle_project_manual_to_scm_transition(
        self, resource_type: str, source_id: int, existing: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Handle transition from Manual to SCM project type.

        AAP requires a two-step update when converting Manual projects to SCM:
        1. Set scm_type and scm_url (basic SCM configuration)
        2. Set scm_update_on_launch and other SCM options

        Args:
            resource_type: Type of resource ('projects')
            source_id: Source resource ID
            existing: Existing project in target AAP
            data: New project data with SCM configuration

        Returns:
            Updated resource or None on failure
        """
        existing_scm_type = existing.get("scm_type") or ""
        new_scm_type = data.get("scm_type") or ""

        # Log for debugging
        logger.debug(
            "checking_project_scm_transition",
            source_id=source_id,
            existing_scm_type=repr(existing_scm_type),
            new_scm_type=repr(new_scm_type),
            is_manual=existing_scm_type in ("", None),
            is_scm=new_scm_type not in ("", None),
        )

        # Check if this is a Manual → SCM transition
        # Manual projects have scm_type as empty string or None
        if existing_scm_type in ("", None) and new_scm_type not in ("", None):
            logger.info(
                "project_manual_to_scm_transition",
                resource_type=resource_type,
                source_id=source_id,
                existing_type="manual",
                new_type=new_scm_type,
            )

            # Step 1: Update with basic SCM fields only
            # Include credential if present (required for private repos)
            basic_scm_data = {
                "scm_type": data.get("scm_type"),
                "scm_url": data.get("scm_url"),
                "scm_branch": data.get("scm_branch", ""),
            }

            # Add credential if present
            if "credential" in data:
                basic_scm_data["credential"] = data["credential"]

            # Remove None values
            basic_scm_data = {k: v for k, v in basic_scm_data.items() if v is not None}

            try:
                logger.info(
                    "project_update_step1_basic_scm",
                    resource_type=resource_type,
                    source_id=source_id,
                    fields=list(basic_scm_data.keys()),
                )
                await self.client.update_resource(resource_type, existing["id"], basic_scm_data)

                # Step 2: Update with SCM options
                scm_options = {
                    "scm_clean": data.get("scm_clean"),
                    "scm_delete_on_update": data.get("scm_delete_on_update"),
                    "scm_update_on_launch": data.get("scm_update_on_launch"),
                    "scm_update_cache_timeout": data.get("scm_update_cache_timeout"),
                }

                # Remove None values
                scm_options = {k: v for k, v in scm_options.items() if v is not None}

                if scm_options:
                    logger.info(
                        "project_update_step2_scm_options",
                        resource_type=resource_type,
                        source_id=source_id,
                        fields=list(scm_options.keys()),
                    )
                    updated = await self.client.update_resource(
                        resource_type, existing["id"], scm_options
                    )
                    return updated
                else:
                    # If no options to set, fetch the updated resource from step 1
                    result = await self.client.get(f"{resource_type}/{existing['id']}/")
                    return result

            except Exception as e:
                logger.error(
                    "project_manual_to_scm_transition_failed",
                    resource_type=resource_type,
                    source_id=source_id,
                    error=str(e),
                )
                raise

        # Not a Manual → SCM transition, return None to indicate no special handling
        return None

    async def _handle_conflict(
        self, resource_type: str, source_id: int, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Handle resource conflict (already exists).

        Args:
            resource_type: Type of resource
            source_id: Source resource ID
            data: Resource data

        Returns:
            Existing resource data or None
        """
        # Try to find existing resource by name
        resource_name = data.get("name")
        if not resource_name:
            return None

        try:
            # For organization-scoped resources, filter by organization to find the
            # correct resource (same name can exist in different organizations)
            organization_id = None
            parent_id = None
            parent_field = None

            if resource_type in ORGANIZATION_SCOPED_RESOURCES:
                organization_id = data.get("organization")
            elif resource_type in PARENT_SCOPED_RESOURCES:
                parent_field = PARENT_SCOPED_RESOURCES[resource_type]
                parent_id = data.get(parent_field)

            existing = await self.client.find_resource_by_name(
                resource_type,
                resource_name,
                organization_id=organization_id,
                parent_id=parent_id,
                parent_field=parent_field,
            )

            if existing:
                # Compare resources to determine action
                resources_match = compare_resources(data, existing)

                if resources_match:
                    # Resources are identical - skip (idempotent)
                    logger.info(
                        "conflict_resolved_skip",
                        resource_type=resource_type,
                        source_id=source_id,
                        reason="Resources match",
                    )
                    self.state.mark_completed(
                        resource_type=resource_type,
                        source_id=source_id,
                        target_id=existing["id"],
                        target_name=existing.get("name"),
                        source_name=data.get("name"),  # Auto-creates record if missing
                    )
                    return existing
                else:
                    # Resources differ - update existing
                    logger.info(
                        "conflict_resolved_update",
                        resource_type=resource_type,
                        source_id=source_id,
                        reason="Resources differ",
                    )

                    # Special handling for projects: Manual → SCM transition
                    if resource_type == "projects":
                        manual_to_scm_result = await self._handle_project_manual_to_scm_transition(
                            resource_type, source_id, existing, data
                        )
                        if manual_to_scm_result:
                            # Transition handled successfully
                            self.state.mark_completed(
                                resource_type=resource_type,
                                source_id=source_id,
                                target_id=manual_to_scm_result["id"],
                                target_name=manual_to_scm_result.get("name"),
                                source_name=data.get("name"),  # Auto-creates record if missing
                            )
                            return manual_to_scm_result

                        # For manual projects, clear SCM options to prevent validation errors
                        # AAP rejects updates that leave scm_update_on_launch=true on manual projects
                        if data.get("scm_type") in ("", None):
                            logger.debug(
                                "clearing_scm_options_for_manual_project",
                                source_id=source_id,
                                existing_scm_update=existing.get("scm_update_on_launch"),
                            )
                            # Explicitly clear SCM options that don't apply to manual projects
                            data = {
                                **data,
                                "scm_update_on_launch": False,
                                "scm_clean": False,
                                "scm_delete_on_update": False,
                                "scm_update_cache_timeout": 0,
                            }

                    # Standard update (or no special handling needed)
                    updated = await self.client.update_resource(resource_type, existing["id"], data)
                    self.state.mark_completed(
                        resource_type=resource_type,
                        source_id=source_id,
                        target_id=updated["id"],
                        target_name=updated.get("name"),
                        source_name=data.get("name"),  # Auto-creates record if missing
                    )
                    return updated

            return None

        except Exception as e:
            logger.error(
                "conflict_resolution_failed",
                resource_type=resource_type,
                source_id=source_id,
                error=str(e),
            )
            return None

    async def _import_parallel(
        self,
        resource_type: str,
        resources: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
        concurrency: int | None = None,
    ) -> list[dict[str, Any]]:
        """Import resources concurrently with live progress updates.

        This method implements parallel import using asyncio.gather() with semaphore
        to limit concurrency. It provides real-time progress updates via the callback.

        Args:
            resource_type: Type of resource (users, teams, etc.)
            resources: List of resources to import
            progress_callback: Optional callback for progress updates.
                Called after each resource with (success_count, failed_count).
            concurrency: Optional override for max concurrent requests.
                Defaults to performance_config.max_concurrent if not specified.

        Returns:
            List of successfully imported resources

        Example:
            >>> def update_progress(success: int, failed: int):
            ...     progress.update_phase(phase_id, success, failed)
            >>> results = await importer._import_parallel(
            ...     "users", users, progress_callback=update_progress
            ... )
        """
        if not resources:
            return []

        # Shared counters (thread-safe with asyncio single-threaded model)
        success_count = 0
        failed_count = 0
        skipped_count = 0
        results = []

        # Semaphore caps concurrency. Explicit 0 must fail fast (a 0-limiter
        # hangs every coroutine); `or` would swallow 0, so check None here.
        raw_concurrent = (
            concurrency if concurrency is not None else self.performance_config.max_concurrent
        )
        try:
            max_concurrent = int(raw_concurrent)
        except (TypeError, ValueError):
            max_concurrent = 5
        if max_concurrent < 1:
            raise ValueError(f"concurrency must be >= 1, got {raw_concurrent!r}")
        semaphore = asyncio.Semaphore(max_concurrent)

        async def import_with_semaphore(resource: dict[str, Any]) -> dict[str, Any] | None:
            """Import a single resource with semaphore control."""
            nonlocal success_count, failed_count, skipped_count
            # Bound before try so malformed rows (None, list) cannot raise
            # UnboundLocalError in the handler and mask the real error.
            source_id: Any = None

            async with semaphore:
                try:
                    if not isinstance(resource, dict):
                        raise TypeError(f"resource must be a dict, got {type(resource).__name__}")
                    # Non-mutating read: never pop caller-owned keys so the
                    # same batch can be re-passed (retry/resume) without
                    # mis-keying state. Strip via a shallow copy instead
                    # (nested values still alias the caller dict; callers
                    # must not mutate nested payloads during import).
                    source_id = resource.get("_source_id", resource.get("id"))
                    payload = {k: v for k, v in resource.items() if k != "_source_id"}

                    # Import resource
                    result = await self.import_resource(
                        resource_type=resource_type,
                        source_id=source_id,
                        data=payload,
                    )

                    # Update counters
                    if result:
                        # Count managed/built-in types as success since mapping was successful
                        # (_skipped means it was mapped but not patched because it's managed)
                        success_count += 1
                        results.append(result)
                    else:
                        # Result is None if skipped (already migrated) or failed
                        # Check if it was skipped (already imported)
                        if not self.state.is_migrated(resource_type, source_id):
                            failed_count += 1
                        # Else: already migrated (skipped), count handled by pre-check logic mostly
                        # But if import_resource returns None for already migrated, we don't track it here
                        # because export_import.py handles pre-check skips.

                    # Update progress after each resource
                    if progress_callback:
                        # Callback expects: success, failed, skipped
                        progress_callback(success_count, failed_count, skipped_count)

                    return result

                except Exception as e:
                    failed_count += 1
                    resource_name = (
                        resource.get("name", "unknown") if isinstance(resource, dict) else "unknown"
                    )

                    # Mark as failed in database (safety net for re-raised exceptions)
                    self.state.mark_failed(
                        resource_type=resource_type,
                        source_id=source_id,
                        error_message=f"{type(e).__name__}: {str(e)}",
                    )

                    # Update progress even on exception
                    if progress_callback:
                        progress_callback(success_count, failed_count, skipped_count)

                    logger.error(
                        "parallel_import_error",
                        resource_type=resource_type,
                        source_id=source_id,
                        source_name=resource_name,
                        error=str(e),
                    )

                    # Track error for reporting
                    self.import_errors.append(
                        {
                            "resource_type": resource_type,
                            "source_id": source_id,
                            "name": resource_name,
                            "error": str(e),
                            "error_type": type(e).__name__,
                        }
                    )

                    return None

        # Create tasks for all resources
        tasks = [import_with_semaphore(resource) for resource in resources]

        # Execute concurrently (limited by semaphore). return_exceptions=True
        # keeps one bad row from cancelling the batch; log returned
        # exceptions since gather itself does not raise them.
        results_or_errors = await asyncio.gather(*tasks, return_exceptions=True)
        for item in results_or_errors:
            if isinstance(item, BaseException):
                logger.error(
                    "parallel_import_task_exception",
                    resource_type=resource_type,
                    error=str(item),
                    error_type=type(item).__name__,
                )

        logger.info(
            "parallel_import_completed",
            resource_type=resource_type,
            total=len(resources),
            success=success_count,
            failed=failed_count,
        )

        return results
