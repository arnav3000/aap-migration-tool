from collections.abc import Callable
from typing import Any

from aap_migration.migration.credential_type_utils import BUILTIN_CREDENTIAL_TYPE_MAX_ID
from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class CredentialImporter(ResourceImporter):
    """Importer for credential resources.

    Credentials are pre-created in the target environment before migration.
    This importer PATCHes existing resources instead of POSTing new ones.
    """

    DEPENDENCIES: dict[str, str] = {
        "organization": "organizations",
        "credential_type": "credential_types",
        "user": "users",
        "team": "teams",
    }

    # Built-in credential type IDs (managed by AAP). Prefer name-based mappings;
    # this ceiling is the fallback when no mapping exists yet.
    BUILTIN_CREDENTIAL_TYPE_MAX_ID = BUILTIN_CREDENTIAL_TYPE_MAX_ID

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
            target_id = self.state.get_mapped_id(resource_type, source_id)
            name = data.get("name") or "unknown"
            if target_id is not None:
                logger.debug(
                    "resource_already_imported",
                    resource_type=resource_type,
                    source_id=source_id,
                    target_id=target_id,
                )
                self.stats["skipped_count"] += 1
                # Return a truthy marker so callers can list the credential for the
                # secret-pause review instead of treating it as a hard skip/failure.
                return {
                    "id": target_id,
                    "name": name,
                    "_already_migrated": True,
                    "_skip_reason": (
                        f"Already migrated (target id {target_id}) — update secrets if needed"
                    ),
                }
            # Stuck in_progress/completed without a target mapping — retry import.
            logger.warning(
                "credential_migrated_without_mapping_retrying",
                resource_type=resource_type,
                source_id=source_id,
                source_name=name,
            )

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

        # Clean up transformer markers
        data.pop("_temp_credential_values", None)
        data.pop("_encrypted_fields", None)
        data.pop("_needs_vault_lookup", None)

        # Capture before resolve — _resolve_dependencies pops _dependency_names.
        dep_names = data.get("_dependency_names")
        source_cred_type_name = None
        if isinstance(dep_names, dict):
            raw_type_name = dep_names.get("credential_type")
            if isinstance(raw_type_name, str) and raw_type_name.strip():
                source_cred_type_name = raw_type_name.strip()

        # Resolve dependencies BEFORE lookup to get target org/credential_type IDs
        # This ensures we can search by the complete composite key
        if resolve_dependencies:
            data = await self._resolve_dependencies(resource_type, data)

        # AAP requires at least one of organization / user / team. When ownership
        # was stripped (unmapped org/user/team) or never present, own the credential
        # as the target's builtin admin user so create can succeed.
        data = await self._ensure_credential_ownership(data)

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
                target_id = int(resources[0]["id"])
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
                    return {
                        "id": target_id,
                        "name": name,
                        "_skipped": True,
                        "_skip_reason": (
                            f"Managed credential on target (id {target_id}) — "
                            "mapped only, not patched"
                        ),
                    }

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
                cred_type_id = data.get("credential_type")
                cred_type_name = await self._lookup_credential_type_name(
                    cred_type_id,
                    {"credential_type": source_cred_type_name} if source_cred_type_name else None,
                )
                input_keys = sorted((data.get("inputs") or {}).keys())
                logger.info(
                    "credential_creating",
                    name=name,
                    source_id=source_id,
                    organization=data.get("organization"),
                    credential_type=cred_type_id,
                    credential_type_name=cred_type_name,
                    input_fields=input_keys,
                    message=(
                        "Creating new credential - no match found for name/org/type "
                        f"composite key (type={cred_type_name or cred_type_id})"
                    ),
                )

                # Dependencies already resolved above before lookup
                # Create resource
                result = await self.client.create_resource(
                    resource_type="credentials",
                    data=data,
                    check_exists=False,  # We already checked with composite key
                )

                target_id = int(result["id"])
                logger.info(
                    "credential_created",
                    name=name,
                    source_id=source_id,
                    target_id=target_id,
                    credential_type=cred_type_id,
                    credential_type_name=cred_type_name,
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
            cred_type_id = data.get("credential_type") if isinstance(data, dict) else None
            cred_type_name = None
            failed_input_keys: list[str] = []
            if isinstance(data, dict):
                failed_input_keys = sorted((data.get("inputs") or {}).keys())
                try:
                    cred_type_name = await self._lookup_credential_type_name(
                        cred_type_id,
                        (
                            {"credential_type": source_cred_type_name}
                            if source_cred_type_name
                            else None
                        ),
                    )
                except Exception:  # nosec B110
                    cred_type_name = source_cred_type_name
            type_label = cred_type_name or cred_type_id or "unknown"
            error_detail = (
                f"{type(e).__name__}: {e} "
                f"(credential_type={type_label}, input_fields={failed_input_keys})"
            )
            logger.error(
                "credential_import_failed",
                source_id=source_id,
                name=name,
                credential_type=cred_type_id,
                credential_type_name=cred_type_name,
                input_fields=failed_input_keys,
                error=str(e),
                message=(
                    f"Failed importing credential '{name}' with credential_type "
                    f"'{type_label}' (id={cred_type_id}); inputs={failed_input_keys}"
                ),
            )
            self.stats["error_count"] += 1

            # Mark as failed in database to prevent stuck "in_progress" state
            self.state.mark_failed(
                resource_type=resource_type,
                source_id=source_id,
                error_message=error_detail,
            )

            self.import_errors.append(
                {
                    "resource_type": resource_type,
                    "source_id": source_id,
                    "name": name,
                    "error": error_detail,
                    "error_type": type(e).__name__,
                    "credential_type": cred_type_id,
                    "credential_type_name": cred_type_name,
                }
            )
            return None

    async def _lookup_credential_type_name(
        self,
        credential_type_id: Any,
        dependency_names: Any = None,
    ) -> str | None:
        """Resolve a human-readable credential type name for logging.

        Prefers stashed source summary names, then id_mappings, then a live
        lookup of the target credential type by id.
        """
        if isinstance(dependency_names, dict):
            name = dependency_names.get("credential_type")
            if isinstance(name, str) and name.strip():
                return name.strip()

        if credential_type_id is None:
            return None

        try:
            type_id = int(credential_type_id)
        except (TypeError, ValueError):
            return None

        try:
            mapping = self.state.get_id_mapping("credential_types", type_id)
        except Exception:
            mapping = None
        if isinstance(mapping, dict):
            for key in ("target_name", "source_name"):
                raw = mapping.get(key)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()

        # Target id may already be remapped — look it up directly on the target.
        try:
            resp = await self.client.get(f"credential_types/{type_id}/")
            if isinstance(resp, dict):
                name = resp.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        except Exception as exc:
            logger.debug(
                "credential_type_name_lookup_failed",
                credential_type_id=type_id,
                error=str(exc),
            )
        return None

    async def _lookup_target_admin_user_id(self) -> int | None:
        """Return the target AAP builtin admin user id, cached per importer instance."""
        cached = getattr(self, "_cached_admin_user_id", None)
        if isinstance(cached, int):
            return cached
        try:
            results = await self.client.get("users/", params={"username": "admin"})
            users = results.get("results") or []
            if users and users[0].get("id") is not None:
                admin_id = int(users[0]["id"])
                self._cached_admin_user_id = admin_id
                return admin_id
        except Exception as exc:
            logger.warning(
                "credential_admin_user_lookup_failed",
                error=str(exc),
                message="Could not look up builtin admin user on target",
            )
        return None

    async def _ensure_credential_ownership(self, data: dict[str, Any]) -> dict[str, Any]:
        """Ensure credential has organization, user, or team ownership for the target API.

        When none remain after dependency resolution, assign ownership to the
        target's builtin admin user.
        """
        if data.get("organization") or data.get("user") or data.get("team"):
            return data

        admin_id = await self._lookup_target_admin_user_id()
        if admin_id is None:
            logger.error(
                "credential_missing_owner_no_admin",
                credential_name=data.get("name"),
                message=(
                    "Credential has no organization/user/team and target admin "
                    "user could not be found"
                ),
            )
            return data

        data["user"] = admin_id
        data.pop("organization", None)
        data.pop("team", None)
        logger.info(
            "credential_owner_defaulted_to_admin",
            credential_name=data.get("name"),
            admin_user_id=admin_id,
            message="Assigned credential to builtin admin user — no org/user/team remaining",
        )
        return data

    async def _resolve_credential_type_by_name(
        self,
        source_id: Any,
        type_name: str | None,
        credential_name: Any = None,
    ) -> int | None:
        """Resolve a credential type on the target by name and cache the mapping.

        Managed/builtin type IDs differ across AAP versions (e.g. Source Control
        is often 2 on Tower and 6 on AAP 2.7). Never assume source ID == target ID.
        """
        if not isinstance(type_name, str) or not type_name.strip():
            return None
        name = type_name.strip()
        try:
            existing = await self.client.find_resource_by_name("credential_types", name)
        except Exception as exc:
            logger.warning(
                "credential_type_name_lookup_failed",
                credential_name=credential_name,
                credential_type_name=name,
                source_id=source_id,
                error=str(exc),
            )
            return None
        if not existing or existing.get("id") is None:
            logger.warning(
                "credential_type_not_found_by_name",
                credential_name=credential_name,
                credential_type_name=name,
                source_id=source_id,
            )
            return None

        target_id = int(existing["id"])
        try:
            self.state.save_id_mapping(
                resource_type="credential_types",
                source_id=int(source_id),
                target_id=target_id,
                source_name=name,
                target_name=existing.get("name") or name,
            )
        except Exception as exc:
            logger.debug(
                "credential_type_mapping_cache_failed",
                source_id=source_id,
                target_id=target_id,
                error=str(exc),
            )
        logger.info(
            "resolved_credential_type_by_name",
            credential_name=credential_name,
            credential_type_name=name,
            source_id=source_id,
            target_id=target_id,
        )
        return target_id

    async def _resolve_dependencies(
        self, resource_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Override to handle built-in credential types.

        Built-in credential types are managed by AAP and not exported because
        they already exist on source and target. Their IDs are **not** stable
        across versions — resolve via id_mappings (from
        ``map_managed_credential_types``) or by type name, never by assuming
        the source ID is valid on the target.

        Custom credential types use normal ID mapping resolution.

        Args:
            resource_type: The resource type being imported
            data: The resource data with source IDs

        Returns:
            Resource data with resolved target IDs
        """
        resolved = dict(data)
        dependency_names = data.get("_dependency_names")
        if not isinstance(dependency_names, dict):
            dependency_names = {}

        # Handle credential_type field specially
        if "credential_type" in data and data["credential_type"]:
            source_id = data["credential_type"]
            target_id = self.state.get_mapped_id("credential_types", source_id)
            type_name = dependency_names.get("credential_type")

            if target_id:
                resolved["credential_type"] = target_id
                logger.debug(
                    "resolved_credential_type_from_mapping",
                    credential_name=data.get("name"),
                    source_id=source_id,
                    target_id=target_id,
                    credential_type_name=type_name,
                )
            else:
                # Prefer name lookup — builtin IDs differ across AAP versions
                # (e.g. Source Control: Tower id=2 vs AAP 2.7 id=6).
                recovered = await self._resolve_credential_type_by_name(
                    source_id=source_id,
                    type_name=type_name if isinstance(type_name, str) else None,
                    credential_name=data.get("name"),
                )
                if recovered is not None:
                    resolved["credential_type"] = recovered
                else:
                    logger.error(
                        "missing_credential_type_mapping",
                        credential_name=data.get("name"),
                        source_id=source_id,
                        credential_type_name=type_name,
                        message=(
                            "credential_type has no id mapping and could not be "
                            "resolved by name; refusing to reuse source ID "
                            "(builtin IDs differ across AAP versions)"
                        ),
                    )
                    resolved.pop("credential_type", None)

        # Resolve other dependencies (organization, user, team) using base logic
        name_prefix = str(data.get("_name_prefix") or self.name_prefix or "")

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
                    recovered_id = await self._recover_dependency_by_name(
                        dep_resource_type=dep_resource_type,
                        dep_source_id=source_id,
                        dep_name=dependency_names.get(field),
                        name_prefix=name_prefix,
                    )
                    if recovered_id is not None:
                        resolved[field] = recovered_id
                        continue
                    logger.warning(
                        "unresolved_dependency",
                        resource_name=data.get("name"),
                        field=field,
                        source_id=source_id,
                        dep_resource_type=dep_resource_type,
                    )
                    # Remove field to allow partial import
                    resolved.pop(field, None)

        resolved.pop("_dependency_names", None)
        resolved.pop("_credential_names", None)
        resolved.pop("_name_prefix", None)

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

        # Clean up transformer marker fields before import
        for credential in credentials:
            credential.pop("_encrypted_fields", None)
            credential.pop("_temp_credential_values", None)

        # All credentials go through the same PATCH flow via import_resource()
        results = await self._import_parallel("credentials", credentials, progress_callback)

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
