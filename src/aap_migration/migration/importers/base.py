"""Base resource importer and shared helpers."""

import asyncio
from collections.abc import Callable
from typing import Any

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.client.exceptions import APIError, ConflictError, DependencyError
from aap_migration.config import PerformanceConfig
from aap_migration.migration.database import get_session
from aap_migration.migration.models import MigrationProgress
from aap_migration.migration.state import MigrationState
from aap_migration.resources import PARENT_SCOPED_RESOURCES
from aap_migration.utils.idempotency import compare_resources
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)

# Resource types that are scoped to organizations
# Names must be unique within an organization, but can duplicate across organizations
ORGANIZATION_SCOPED_RESOURCES = {
    "teams",
    "projects",
    "inventories",
    "credentials",
    "job_templates",
    "workflow_job_templates",
    "notification_templates",
}

# Resource types that REQUIRE an organization (cannot be global/None)
# These resources must have organization field populated to be created in AAP
# Note: job_templates and workflow_job_templates inherit org from project/inventory
#       credentials can be global or org-scoped (organization is optional for both)
ORGANIZATION_REQUIRED_RESOURCES = {
    "teams",  # Must have org
    "projects",  # Must have org
    "inventories",  # Must have org
    "notification_templates",  # Must have org
}


class ResourceImporter:
    """Base class for importing resources to AAP 2.6.

    Handles dependency resolution, conflict detection, and state tracking.
    """

    # Dependency mapping: field_name -> resource_type
    DEPENDENCIES: dict[str, str] = {}

    # Identifier field used for uniqueness checks (override in subclasses if different)
    IDENTIFIER_FIELD = "name"

    def __init__(
        self,
        client: AAPTargetClient,
        state: MigrationState,
        performance_config: PerformanceConfig,
        resource_mappings: dict[str, dict[str, str]] | None = None,
        name_prefix: str = "",
    ):
        """Initialize resource importer.

        Args:
            client: AAP target client instance
            state: Migration state manager
            performance_config: Performance configuration
            resource_mappings: Optional resource name mappings from config/mappings.yaml
            name_prefix: Optional source name prefix (for FK recovery by prefixed name)
        """
        self.client = client
        self.state = state
        self.performance_config = performance_config
        self.resource_mappings = resource_mappings or {}
        self.name_prefix = name_prefix or ""
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
                    unresolved_org = data.pop("_unresolved_required_organization", None)
                    name = data.get("name", "unknown")
                    if unresolved_org is not None:
                        error_msg = (
                            f"Organization (source id {unresolved_org}) was not migrated for "
                            f"{resource_type} '{name}' (source ID {source_id}). Include that "
                            f"organization in the plan or migrate it first."
                        )
                    else:
                        error_msg = (
                            f"Missing required field 'organization' for {resource_type}. "
                            f"Resource '{name}' (source ID {source_id}) "
                            f"cannot be created without an organization. This often means the "
                            f"organization was only present in summary_fields and was not "
                            f"extracted, or the source record has no organization."
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
                data.pop("_unresolved_required_organization", None)
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
                            action="mapping_existing_target_resource",
                        )
                        # Map source → existing target. Prefer mark_completed so
                        # is_migrated / get_mapped_id work for dependents (projects, etc.).
                        self.state.mark_completed(
                            resource_type=resource_type,
                            source_id=source_id,
                            target_id=int(existing["id"]),
                            target_name=existing.get("name"),
                            source_name=resource_name,
                        )
                        self.stats["skipped_count"] += 1
                        return {
                            **existing,
                            "_already_migrated": True,
                            "_skip_reason": (
                                f"Already exists on target (id {existing['id']}) — "
                                f"mapped to source id {source_id}"
                            ),
                        }
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
                error_msg = f"Conflict ({type(e).__name__}): {str(e)}"
                self.state.mark_failed(
                    resource_type=resource_type,
                    source_id=source_id,
                    error_message=error_msg,
                )
                self._record_import_failure(
                    resource_type,
                    source_id,
                    data.get("name", "unknown"),
                    error_msg,
                    error_type=type(e).__name__,
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
                    error_msg = f"Already exists ({type(e).__name__}): {str(e)}"
                    self.state.mark_failed(
                        resource_type=resource_type,
                        source_id=source_id,
                        error_message=error_msg,
                    )
                    self._record_import_failure(
                        resource_type,
                        source_id,
                        data.get("name", "unknown"),
                        error_msg,
                        error_type=type(e).__name__,
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
                self._record_import_failure(
                    resource_type,
                    source_id,
                    data.get("name", "unknown"),
                    enriched_error,
                    error_type=type(e).__name__,
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

        # Get dependencies for this resource type (may be empty for some importers)
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
                            assert dep_resource_type is not None
                            enriched_parts.append(
                                f"{field}: {dep_resource_type.rstrip('s').replace('_', ' ').title()} "
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

    def _infer_resource_type_from_field(self, field_name: str) -> str | None:
        """Infer resource type from field name.

        Args:
            field_name: Field name from API error

        Returns:
            Inferred resource type or None
        """
        # Map common field names to resource types
        field_to_resource_type = {
            "inventory": "inventories",
            "project": "projects",
            "organization": "organizations",
            "credential": "credentials",
            "webhook_credential": "credentials",
            "execution_environment": "execution_environments",
            "instance_group": "instance_groups",
            "job_template": "job_templates",
            "workflow_job_template": "workflow_job_templates",
            "unified_job_template": "job_templates",  # Could be job or workflow, assume job
        }

        return field_to_resource_type.get(field_name)

    def _get_dependency_name(self, resource_type: str, source_id: int) -> str | None:
        """Get the source name of a dependency resource from the database.

        Args:
            resource_type: Type of dependency resource
            source_id: Source ID of the dependency

        Returns:
            Source resource name or None if not found
        """
        try:
            with get_session(self.state.database_url) as session:
                progress = (
                    session.query(MigrationProgress)
                    .filter_by(resource_type=resource_type, source_id=source_id)
                    .first()
                )

                if progress and progress.source_name:
                    return str(progress.source_name)

        except Exception as e:
            logger.debug(
                "dependency_name_lookup_failed",
                resource_type=resource_type,
                source_id=source_id,
                error=str(e),
            )

        return None

    def _add_notification_warnings(
        self, resource_type: str, warnings_by_source_id: dict[int, list[str]]
    ) -> None:
        """Add notification association warnings to resource records in database.

        Updates the error_message field for completed resources to include warnings
        about incomplete notification associations. These warnings appear in migration reports.

        Args:
            resource_type: Type of resource (job_templates, workflow_job_templates)
            warnings_by_source_id: Dict mapping source_id -> list of warning messages
        """
        try:
            from aap_migration.migration.database import get_session

            with get_session(self.state.database_url) as session:
                for source_id, warnings in warnings_by_source_id.items():
                    progress = (
                        session.query(MigrationProgress)
                        .filter_by(resource_type=resource_type, source_id=source_id)
                        .first()
                    )

                    if progress and progress.status == "completed":
                        # Append warnings to existing error_message
                        warning_text = "WARNING: " + "; ".join(warnings)
                        if progress.error_message:
                            progress.error_message = f"{progress.error_message}\n{warning_text}"
                        else:
                            progress.error_message = warning_text

                        logger.info(
                            "notification_warning_added_to_report",
                            resource_type=resource_type,
                            source_id=source_id,
                            source_name=progress.source_name,
                            warning_count=len(warnings),
                        )

                session.commit()

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

        When an ID mapping is missing, attempt recovery by looking up the related
        target resource by name — trying the configured ``name_prefix`` first so
        prepended sources still attach credentials/projects/etc.

        Args:
            resource_type: Type of resource
            data: Resource data

        Returns:
            Data with resolved dependencies
        """
        resolved = dict(data)
        dependencies = self._get_dependencies(resource_type)
        resource_source_id = data.get("_source_id") or data.get("id")
        name_prefix = str(data.get("_name_prefix") or self.name_prefix or "")
        dependency_names = data.get("_dependency_names")
        if not isinstance(dependency_names, dict):
            dependency_names = {}
        unresolved_fields: list[str] = []

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
                    # Scope name lookups by target organization when we have one.
                    # Never treat a still-unmapped source org id as a target filter.
                    lookup_org_id = None
                    if field != "organization":
                        org_val = resolved.get("organization")
                        if org_val is not None:
                            mapped_org = self.state.get_mapped_id("organizations", org_val)
                            if mapped_org is not None:
                                lookup_org_id = mapped_org
                            elif org_val != data.get("organization"):
                                # Already rewritten to a target id earlier in this pass
                                lookup_org_id = org_val

                    recovered_id = await self._recover_dependency_by_name(
                        dep_resource_type=dep_resource_type,
                        dep_source_id=dep_source_id,
                        dep_name=dependency_names.get(field),
                        name_prefix=name_prefix,
                        organization_id=lookup_org_id,
                    )
                    if recovered_id is not None:
                        resolved[field] = recovered_id
                        logger.info(
                            "dependency_recovered_by_name",
                            resource_type=resource_type,
                            source_id=resource_source_id,
                            field=field,
                            dep_resource_type=dep_resource_type,
                            dep_source_id=dep_source_id,
                            target_id=recovered_id,
                            dep_name=dependency_names.get(field),
                            name_prefix=name_prefix or None,
                        )
                        continue

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
                        dep_name=dependency_names.get(field),
                        name_prefix=name_prefix or None,
                    )

                    # Org field keeps special handling for validation error messages below.
                    if resource_type in ORGANIZATION_REQUIRED_RESOURCES and field == "organization":
                        resolved["_unresolved_required_organization"] = dep_source_id
                        resolved.pop(field, None)
                    else:
                        unresolved_fields.append(field)
                        resolved.pop(field, None)

        if unresolved_fields:
            raise DependencyError(
                f"Unresolved dependencies for {resource_type} "
                f"(source_id={resource_source_id}): {', '.join(unresolved_fields)}"
            )

        resolved.pop("_dependency_names", None)
        resolved.pop("_credential_names", None)
        resolved.pop("_name_prefix", None)

        return resolved

    async def _recover_dependency_by_name(
        self,
        *,
        dep_resource_type: str,
        dep_source_id: Any,
        dep_name: str | None,
        name_prefix: str,
        organization_id: Any = None,
        parent_field: str | None = None,
        parent_id: Any = None,
    ) -> int | None:
        """Find a related target resource by name when ID mapping is missing.

        Tries the prefixed name first (when ``name_prefix`` is set), then the
        original source name. On success, persists an ID mapping for later use.

        When ``dep_name`` is missing (sparse summary_fields), fall back to the
        name recorded in migration progress / id_mappings from the dependency's
        own import — critical for attaching project SCM credentials after a
        name-prefixed credential import.
        """
        if not dep_name:
            try:
                sid = int(dep_source_id)
            except (TypeError, ValueError):
                sid = None
            if sid is not None:
                dep_name = self._get_dependency_name(dep_resource_type, sid)
                if not dep_name:
                    try:
                        mapping = self.state.get_id_mapping(dep_resource_type, sid)
                    except Exception:
                        mapping = None
                    if isinstance(mapping, dict):
                        raw = mapping.get("source_name") or mapping.get("target_name")
                        if isinstance(raw, str) and raw.strip():
                            dep_name = raw.strip()
        if not dep_name:
            return None

        # Prefer prefixed name when configured. If the recorded name already has
        # the prefix (from credential import), try it first then the bare name.
        candidates: list[str] = []
        if name_prefix and dep_name.startswith(name_prefix):
            candidates.append(dep_name)
            bare = dep_name[len(name_prefix) :]
            if bare:
                candidates.append(bare)
        elif name_prefix:
            candidates.append(f"{name_prefix}{dep_name}")
            candidates.append(dep_name)
        else:
            candidates.append(dep_name)

        org_id = None
        if organization_id is not None:
            try:
                org_id = int(organization_id)
            except (TypeError, ValueError):
                org_id = None

        # Try org-scoped lookup first, then global — credentials may be
        # admin/user-owned (organization=null) after ownership fallback.
        org_attempts: list[int | None] = [org_id] if org_id is not None else [None]
        if org_id is not None:
            org_attempts.append(None)

        for candidate in candidates:
            for attempt_org in org_attempts:
                try:
                    existing = await self.client.find_resource_by_name(
                        dep_resource_type,
                        candidate,
                        organization_id=attempt_org,
                        parent_id=parent_id,
                        parent_field=parent_field,
                    )
                except Exception as exc:
                    logger.debug(
                        "dependency_name_lookup_failed",
                        dep_resource_type=dep_resource_type,
                        name=candidate,
                        organization_id=attempt_org,
                        error=str(exc),
                    )
                    continue

                if not existing or existing.get("id") is None:
                    continue

                target_id = int(existing["id"])
                try:
                    self.state.save_id_mapping(
                        resource_type=dep_resource_type,
                        source_id=int(dep_source_id),
                        target_id=target_id,
                        source_name=dep_name,
                        target_name=existing.get("name") or candidate,
                    )
                except Exception as exc:
                    logger.debug(
                        "dependency_name_mapping_persist_failed",
                        dep_resource_type=dep_resource_type,
                        dep_source_id=dep_source_id,
                        target_id=target_id,
                        error=str(exc),
                    )
                return target_id

        return None

    def _get_dependencies(self, resource_type: str) -> dict[str, str]:
        """Get dependency mapping for resource type.

        Args:
            resource_type: Type of resource

        Returns:
            Dictionary mapping field names to resource types
        """
        # Use class-level DEPENDENCIES or return empty dict
        return self.DEPENDENCIES

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

        organization_id = None
        parent_id = None
        parent_field = None
        skip_lookup = False

        if resource_type in ORGANIZATION_SCOPED_RESOURCES:
            organization_id = data.get("organization")
            if organization_id is None:
                skip_lookup = True
        elif resource_type in PARENT_SCOPED_RESOURCES:
            parent_field = PARENT_SCOPED_RESOURCES[resource_type]
            parent_id = data.get(parent_field)
            if parent_id is None:
                skip_lookup = True

        try:
            existing = None
            if not skip_lookup:
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

        # Semaphore limits concurrent requests (use override or default)
        max_concurrent = concurrency or self.performance_config.max_concurrent
        semaphore = asyncio.Semaphore(max_concurrent)

        async def import_with_semaphore(resource: dict[str, Any]) -> dict[str, Any] | None:
            """Import a single resource with semaphore control."""
            nonlocal success_count, failed_count, skipped_count

            async with semaphore:
                try:
                    # Extract source ID
                    source_id = resource.pop("_source_id", resource.get("id"))

                    # Import resource
                    result = await self.import_resource(
                        resource_type=resource_type,
                        source_id=source_id,
                        data=resource,
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
                        source_name=resource.get("name", "unknown"),
                        error=str(e),
                    )

                    # Track error for reporting
                    self.import_errors.append(
                        {
                            "resource_type": resource_type,
                            "source_id": source_id,
                            "name": resource.get("name", "unknown"),
                            "error": str(e),
                            "error_type": type(e).__name__,
                        }
                    )

                    return None

        # Create tasks for all resources
        tasks = [import_with_semaphore(resource) for resource in resources]

        # Execute concurrently (limited by semaphore)
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info(
            "parallel_import_completed",
            resource_type=resource_type,
            total=len(resources),
            success=success_count,
            failed=failed_count,
        )

        return results

    def get_stats(self) -> dict[str, int]:
        """Get import statistics.

        Returns:
            Dictionary with import statistics
        """
        return self.stats.copy()

    def reset_stats(self) -> None:
        """Reset import statistics."""
        self.stats = {
            "imported_count": 0,
            "error_count": 0,
            "conflict_count": 0,
            "skipped_count": 0,
        }

    def get_import_errors(self) -> list[dict[str, Any]]:
        """Get list of import errors for reporting.

        Returns:
            List of error dictionaries with resource details including:
            - resource_type: Type of resource that failed
            - source_id: Source resource ID
            - name: Resource name
            - error: Error message
            - error_type: Exception type name
        """
        return self.import_errors.copy()

    def _failure_detail_for_resource(
        self,
        resource_type: str,
        source_id: int,
        fallback: str = "import returned no result",
    ) -> str:
        """Resolve a human-readable failure reason from errors list or state DB."""
        for err in reversed(self.import_errors):
            if err.get("resource_type") == resource_type and err.get("source_id") == source_id:
                return str(err.get("error", fallback))
        state_error = self.state.get_error_message(resource_type, source_id)
        if state_error:
            return state_error
        return fallback

    def _record_import_failure(
        self,
        resource_type: str,
        source_id: int,
        name: str,
        error: str,
        error_type: str = "ImportError",
    ) -> None:
        """Append a structured import failure for reporting (idempotent per source id)."""
        for err in self.import_errors:
            if err.get("resource_type") == resource_type and err.get("source_id") == source_id:
                return
        self.import_errors.append(
            {
                "resource_type": resource_type,
                "source_id": source_id,
                "name": name,
                "error": error,
                "error_type": error_type,
            }
        )
