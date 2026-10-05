"""Domain resource importers (split from migration.importer; re-exported)."""

from collections.abc import Callable
from typing import Any, cast

from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class NotificationTemplateImporter(ResourceImporter):
    """Importer for notification template resources.

    Notification templates define how AAP sends notifications about
    job status (email, Slack, webhook, etc.).
    """

    DEPENDENCIES = {
        "organization": "organizations",
    }

    async def import_notification_templates(
        self,
        notifications: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple notification templates concurrently with live progress updates.

        Args:
            notifications: List of notification template data
            progress_callback: Optional callback for progress updates.
                Called after each notification with (success_count, failed_count).

        Returns:
            List of created notification template data
        """
        return await self._import_parallel(
            "notification_templates", notifications, progress_callback
        )


class SystemJobTemplateImporter(ResourceImporter):
    """Importer for system job template resources.

    System job templates are built-in and read-only. We only map them.
    """

    DEPENDENCIES: dict[str, str] = {}

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Map system job template by name."""
        if self.state.is_migrated(resource_type, source_id):
            self.stats["skipped_count"] += 1
            return None

        name = data.get("name")
        if not name:
            return None

        self.state.mark_in_progress(resource_type, source_id, name, "import")

        try:
            # Lookup by name
            results = await self.client.get(
                "system_job_templates/",
                params={"name": name},
            )
            resources = results.get("results", [])

            if resources:
                target_id = resources[0]["id"]
                self.state.save_id_mapping(
                    resource_type=resource_type,
                    source_id=source_id,
                    target_id=target_id,
                    source_name=name,
                    target_name=name,
                )
                self.state.mark_completed(resource_type, source_id, target_id, name)
                self.stats["imported_count"] += 1
                logger.info(
                    "system_job_template_mapped",
                    source_id=source_id,
                    target_id=target_id,
                    name=name,
                )
                return {"id": target_id, "name": name}
            else:
                logger.warning(
                    "system_job_template_not_found_in_target",
                    name=name,
                    source_id=source_id,
                )
                self.state.mark_failed(resource_type, source_id, "Not found in target")
                self.stats["error_count"] += 1
                return None

        except Exception as e:
            logger.error(
                "system_job_template_import_failed",
                name=name,
                error=str(e),
            )
            self.state.mark_failed(resource_type, source_id, str(e))
            self.stats["error_count"] += 1
            return None

    async def import_system_job_templates(
        self,
        templates: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple system job templates (mapping only)."""
        # Extract schedules before import (non-mutating: keep caller dicts
        # intact so a second pass still sees _source_id/schedules).
        templates_with_schedules = []
        for template in templates:
            schedules = template.get("schedules", None)
            if schedules:
                source_id = template.get("_source_id", template.get("id"))
                templates_with_schedules.append(
                    {
                        "source_template_id": source_id,
                        "schedules": schedules,
                    }
                )

        # Import (map) system job templates without the nested schedules key.
        clean_templates = [{k: v for k, v in t.items() if k != "schedules"} for t in templates]
        results = await self._import_parallel(
            "system_job_templates", clean_templates, progress_callback
        )

        # Import schedules for successfully mapped system job templates
        if templates_with_schedules:
            logger.info(
                "importing_system_job_template_schedules",
                total_templates_with_schedules=len(templates_with_schedules),
            )

            for schedule_data in templates_with_schedules:
                source_template_id = schedule_data["source_template_id"]
                schedules = schedule_data["schedules"]

                # Get the target system job template ID from the state mapping
                target_template_id = self.state.get_mapped_id(
                    "system_job_templates", source_template_id
                )
                if not target_template_id:
                    logger.warning(
                        "system_job_template_not_found_for_schedule",
                        source_template_id=source_template_id,
                    )
                    continue

                # Get system job template name for logging
                template_result = next(
                    (t for t in results if t.get("id") == target_template_id), None
                )
                template_name = (
                    template_result.get("name", "unknown") if template_result else "unknown"
                )

                for schedule in schedules:
                    schedule_name = schedule.get("name", "unknown")

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

                    try:
                        result = await self.client.post(
                            f"system_job_templates/{target_template_id}/schedules/",
                            json_data=schedule_to_import,
                        )
                        logger.info(
                            "system_job_template_schedule_imported",
                            template_id=target_template_id,
                            template_name=template_name,
                            schedule_name=schedule_name,
                            schedule_id=result.get("id"),
                        )
                    except Exception as e:
                        logger.error(
                            "system_job_template_schedule_import_failed",
                            template_id=target_template_id,
                            template_name=template_name,
                            schedule_name=schedule_name,
                            error=str(e),
                        )

        return results


class CredentialInputSourceImporter(ResourceImporter):
    """Importer for credential input source resources.

    Credential input sources link credential input fields to values
    from other credentials (e.g., a Vault credential).
    """

    DEPENDENCIES = {
        "credential": "credentials",  # The credential being modified
        "source_credential": "credentials",  # The credential providing the input
    }

    async def import_credential_input_sources(
        self,
        input_sources: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple credential input sources by patching existing credentials.

        This importer does not create new resources. Instead, it modifies the `inputs`
        field of an existing credential to link it to another source credential.

        Args:
            input_sources: List of credential input source data
            progress_callback: Optional callback for progress updates.

        Returns:
            List of patched credential data
        """
        results = []
        # Removed local success_count, failed_count, skipped_count

        for input_source in input_sources:
            # Non-mutating read (see base._import_parallel): keep _source_id
            # on the caller dict so retries re-key correctly.
            source_id = input_source.get("_source_id", input_source.get("id"))
            # `credential` is the ID of the credential whose input is being sourced.
            source_target_credential_id = input_source.get(
                "credential"
            )  # Renamed for clarity to avoid confusion with the source_credential for the input value.
            source_input_field_name = input_source.get("input_field_name")
            # `source_credential` is the ID of the credential that provides the source (e.g., a HashiCorp Vault credential).
            source_source_credential_id = input_source.get("source_credential")
            source_source_credential_field_name = input_source.get("source_credential_field_name")

            if not all(
                [
                    source_target_credential_id,
                    source_input_field_name,
                    source_source_credential_id,
                    source_source_credential_field_name,
                ]
            ):
                logger.warning(
                    "credential_input_source_missing_fields",
                    source_id=source_id,
                    message="Skipping credential input source due to missing required fields",
                )
                self.stats["error_count"] += 1
                if progress_callback:
                    progress_callback(
                        self.stats["imported_count"],
                        self.stats["error_count"],
                        self.stats["skipped_count"],
                    )
                continue

            # Check if the credential associated with this input source has already been migrated.
            target_credential_id = self.state.get_mapped_id(
                "credentials", cast(int, source_target_credential_id)
            )
            if not target_credential_id:
                logger.warning(
                    "credential_input_source_target_credential_not_imported",
                    source_id=source_id,
                    target_credential_id=source_target_credential_id,
                    message="Skipping credential input source - target credential not found",
                )
                self.stats["error_count"] += 1
                if progress_callback:
                    progress_callback(
                        self.stats["imported_count"],
                        self.stats["error_count"],
                        self.stats["skipped_count"],
                    )
                continue

            # Resolve the source_credential to its target ID
            target_source_credential_id = self.state.get_mapped_id(
                "credentials", cast(int, source_source_credential_id)
            )
            if not target_source_credential_id:
                logger.warning(
                    "credential_input_source_source_credential_not_imported",
                    source_id=source_id,
                    source_credential_id=source_source_credential_id,
                    message="Skipping credential input source - source credential not found",
                )
                self.stats["error_count"] += 1
                if progress_callback:
                    progress_callback(
                        self.stats["imported_count"],
                        self.stats["error_count"],
                        self.stats["skipped_count"],
                    )
                continue

            # Construct the value to patch into the target credential's 'inputs'
            # Format: "$<target_source_credential_id>.<source_credential_field_name>$"
            new_input_value = (
                f"${target_source_credential_id}.{source_source_credential_field_name}$"
            )

            try:
                # Fetch the target credential to get its current inputs
                # This is a GET, then PATCH - ensures other inputs are preserved
                target_credential_obj = await self.client.get(
                    f"credentials/{target_credential_id}/"
                )
                current_inputs = target_credential_obj.get("inputs", {})

                # Update the specific input field
                current_inputs[source_input_field_name] = new_input_value

                # Patch the target credential with the updated inputs
                # Note: This is an important distinction: we modify an existing resource,
                # not create a new one. The target_id for the state mapping will be
                # the ID of the credential that was patched.
                await self.client.patch(
                    f"credentials/{target_credential_id}/",
                    json_data={"inputs": current_inputs},
                )

                # Mark as completed (even though it's a PATCH, not CREATE)
                self.state.mark_completed(
                    resource_type="credential_input_sources",
                    source_id=source_id,
                    target_id=target_credential_id,  # Link to the patched credential
                    source_name=input_source.get("name", f"CIS-{source_id}"),
                    target_name=target_credential_obj.get("name"),
                )
                self.stats["imported_count"] += 1
                results.append(
                    {"id": target_credential_id, "name": target_credential_obj.get("name")}
                )
                logger.info(
                    "credential_input_source_patched",
                    source_id=source_id,
                    target_credential_id=target_credential_id,
                    input_field=source_input_field_name,
                    new_input_value=new_input_value,
                )

            except Exception as e:
                self.stats["error_count"] += 1
                logger.error(
                    "credential_input_source_patch_failed",
                    source_id=source_id,
                    target_credential_id=target_credential_id,
                    error=str(e),
                    exc_info=True,
                )
                self.state.mark_failed(
                    resource_type="credential_input_sources",
                    source_id=source_id,
                    error_message=str(e),
                )

            if progress_callback:
                progress_callback(
                    self.stats["imported_count"],
                    self.stats["error_count"],
                    self.stats["skipped_count"],
                )

        return results


# Factory function for creating importers
class ApplicationImporter(ResourceImporter):
    """Importer for OAuth applications with secret management.

    Applications contain sensitive client secrets. This importer:
    - Auto-generates new client secrets (security best practice)
    - Creates applications with new secrets
    - Generates report of which external systems need updates
    - Optionally uses provided secrets from config
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
        """Import an OAuth application with secret generation.

        Args:
            resource_type: Should be 'applications'
            source_id: Source application ID
            data: Application data
            resolve_dependencies: Whether to resolve organization dependency

        Returns:
            Created application data with new client_id/client_secret
        """
        if self.state.is_migrated(resource_type, source_id):
            self.stats["skipped_count"] += 1
            return None

        name = data.get("name")
        if not name:
            logger.error("application_missing_name", source_id=source_id)
            return None

        self.state.mark_in_progress(resource_type, source_id, name, "import")

        # Work on a copy so caller-owned dicts are never mutated.
        data = dict(data)
        # Resolve organization dependency
        if resolve_dependencies:
            data = await self._resolve_dependencies(resource_type, data)

        # Handle client secret
        if data.get("_requires_new_secret"):
            # Client secret will be auto-generated by AAP on creation
            # Remove the redacted placeholder
            data.pop("client_secret", None)
            logger.info("application_will_generate_new_secret", name=name, source_id=source_id)

        # Remove fields that AAP auto-generates or shouldn't be sent in POST
        # client_id and client_secret are auto-generated by AAP
        data.pop("client_id", None)
        if not data.get("_requires_new_secret"):
            # Also remove client_secret if it exists (AAP masks it anyway)
            data.pop("client_secret", None)

        # Remove migration metadata
        for key in list(data.keys()):
            if key.startswith("_"):
                data.pop(key)

        # Create application
        try:
            result = await self.client.post(f"{resource_type}/", json_data=data)

            target_id = result["id"]
            new_client_id = result.get("client_id")
            new_client_secret = result.get("client_secret")

            # Save mapping
            self.state.save_id_mapping(
                resource_type=resource_type,
                source_id=source_id,
                target_id=target_id,
                source_name=name,
                target_name=result.get("name", name),
            )
            self.state.mark_completed(resource_type, source_id, target_id, name)
            self.stats["imported_count"] += 1

            # Log new credentials for user
            logger.info(
                "application_created_with_new_secret",
                source_id=source_id,
                target_id=target_id,
                name=name,
                client_id=new_client_id,
                message="⚠️  Update external systems with new credentials",
            )

            # Add to report for user
            self.import_errors.append(
                {
                    "resource_type": "applications",
                    "source_id": source_id,
                    "name": name,
                    "action_required": "UPDATE_EXTERNAL_SYSTEMS",
                    "new_client_id": new_client_id,
                    "new_client_secret": new_client_secret,
                    "message": f"Application '{name}' created with NEW credentials. Update external systems.",
                }
            )

            return result

        except Exception as e:
            logger.error(
                "application_import_failed",
                name=name,
                source_id=source_id,
                error=str(e),
            )
            self.state.mark_failed(resource_type, source_id, str(e))
            self.stats["error_count"] += 1
            return None

    async def import_applications(
        self,
        applications: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple OAuth applications.

        Args:
            applications: List of application data
            progress_callback: Optional progress callback

        Returns:
            List of created applications with new secrets
        """
        return await self._import_parallel("applications", applications, progress_callback)
