"""Domain resource importers (split from migration.importer; re-exported)."""

from collections.abc import Callable
from typing import Any, cast

from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class JobTemplateImporter(ResourceImporter):
    """Importer for job template resources."""

    DEPENDENCIES = {
        "organization": "organizations",
        "inventory": "inventories",
        "project": "projects",
        "credential": "credentials",
        "execution_environment": "execution_environments",
        "webhook_credential": "credentials",  # Webhook credential (PAT for GitHub/GitLab)
    }

    async def import_resource(
        self,
        resource_type: str,
        source_id: int | str,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Import a job template with credential association.

        Overrides base method to handle the `credentials` field after
        the job template is created.

        Args:
            resource_type: Resource type (should be "job_templates")
            source_id: Source resource ID
            data: Resource data to import

        Returns:
            Created/updated resource data, or None if failed
        """
        # Work on a copy so caller-owned dicts are never mutated (retry
        # passes reuse the same batch).
        data = dict(data)
        # Extract credentials before import (they're not valid API fields)
        credentials = data.pop("credentials", [])
        template_name = data.get("name")

        # Call base import_resource
        result = await super().import_resource(resource_type, cast(int, source_id), data)

        # Associate credentials if import succeeded and we have credentials
        if result and result.get("id") and credentials:
            logger.info(
                "associating_credentials_with_job_template",
                job_template_id=result["id"],
                template_name=template_name,
                credential_count=len(credentials),
            )
            await self._associate_credentials(result["id"], credentials, template_name)

        return result

    async def import_job_templates(
        self,
        templates: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import job templates with credential associations.

        This method imports job templates sequentially to handle post-creation
        credential associations via the `/job_templates/{id}/credentials/` endpoint.

        Args:
            templates: List of job template data
            progress_callback: Optional callback for progress updates.
                Called after each job template with (success_count, failed_count).

        Returns:
            List of created job template data
        """
        results = []
        success_count = 0
        failed_count = 0
        skipped_count = 0
        templates_with_schedules = []  # Collect templates that have schedules to create
        templates_with_surveys = []  # Collect templates that have surveys to create
        templates_with_notifications = []  # Collect templates that have notification associations

        for template in templates:
            # Non-mutating reads: keep caller dicts intact for retry passes;
            # build a stripped copy for the API call instead of popping.
            source_id = template.get("_source_id", template.get("id"))

            # Extract credentials for post-creation association
            credentials = template.get("credentials", [])

            # Extract schedules for separate import
            schedules = template.get("schedules", None)

            # Extract survey spec for separate import (must be POSTed after template creation)
            survey_spec = template.get("survey_spec", None)

            # Extract notification associations for separate import
            notifications = template.get("notifications", None)

            # Clean up EE markers
            if template.get("_needs_execution_environment"):
                logger.warning(
                    "job_template_needs_ee_mapping",
                    resource_type="job_templates",
                    source_id=source_id,
                    source_name=template.get("name"),
                    virtualenv=template.get("_custom_virtualenv_path"),
                )
            template_payload = {
                k: v
                for k, v in template.items()
                if k
                not in (
                    "_source_id",
                    "credentials",
                    "schedules",
                    "survey_spec",
                    "notifications",
                    "_needs_execution_environment",
                    "_custom_virtualenv_path",
                )
            }

            try:
                # Create the job template
                result = await self.import_resource(
                    resource_type="job_templates",
                    source_id=source_id,
                    data=template_payload,
                )

                if result:
                    target_id = result["id"]

                    # Associate credentials after creation
                    if credentials:
                        await self._associate_credentials(
                            target_id, credentials, template.get("name")
                        )

                    # Store schedules for later import
                    if schedules:
                        templates_with_schedules.append(
                            {
                                "source_template_id": source_id,
                                "template_id": target_id,
                                "template_name": result.get("name", "unknown"),
                                "schedules": schedules,
                            }
                        )

                    # Store survey spec for later import
                    if survey_spec:
                        templates_with_surveys.append(
                            {
                                "source_template_id": source_id,
                                "template_id": target_id,
                                "template_name": result.get("name", "unknown"),
                                "survey_spec": survey_spec,
                            }
                        )

                    # Store notification associations for later import
                    if notifications:
                        templates_with_notifications.append(
                            {
                                "source_template_id": source_id,
                                "template_id": target_id,
                                "template_name": result.get("name", "unknown"),
                                "notifications": notifications,
                            }
                        )

                    results.append(result)
                    success_count += 1
                else:
                    failed_count += 1

            except Exception as e:
                failed_count += 1

                # Mark as failed in database
                self.state.mark_failed(
                    resource_type="job_templates",
                    source_id=source_id,
                    error_message=f"{type(e).__name__}: {str(e)}",
                )

                logger.error(
                    "job_template_import_failed",
                    source_id=source_id,
                    name=template.get("name"),
                    error=str(e),
                )

            if progress_callback:
                progress_callback(success_count, failed_count, skipped_count)

        # Import surveys BEFORE schedules — augmented extra_data provides all
        # required survey defaults so AAP 2.6 can validate schedule creation.
        if templates_with_surveys:
            logger.info(
                "importing_job_template_surveys",
                total_surveys=len(templates_with_surveys),
            )

            for survey_data in templates_with_surveys:
                source_template_id = survey_data["source_template_id"]
                template_id = survey_data["template_id"]
                template_name = survey_data["template_name"]
                survey_spec = survey_data["survey_spec"]

                try:
                    await self.client.post(
                        f"job_templates/{template_id}/survey_spec/",
                        json_data=survey_spec,
                    )
                    logger.info(
                        "job_template_survey_imported",
                        template_id=template_id,
                        template_name=template_name,
                        survey_questions=len(survey_spec.get("spec", [])),
                    )
                except Exception as e:
                    logger.error(
                        "job_template_survey_import_failed",
                        template_id=template_id,
                        template_name=template_name,
                        error=str(e),
                    )

        # Import schedules AFTER surveys
        if templates_with_schedules:
            logger.info(
                "importing_job_template_schedules",
                total_templates_with_schedules=len(templates_with_schedules),
            )

            for schedule_data in templates_with_schedules:
                source_template_id = schedule_data["source_template_id"]
                template_id = schedule_data["template_id"]
                template_name = schedule_data["template_name"]
                schedules = schedule_data["schedules"]

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
                            f"job_templates/{template_id}/schedules/",
                            json_data=schedule_to_import,
                        )
                        logger.info(
                            "job_template_schedule_imported",
                            template_id=template_id,
                            template_name=template_name,
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
                                    "job_template_schedule_tracked",
                                    source_id=source_schedule_id,
                                    target_id=result.get("id"),
                                    schedule_name=schedule_name,
                                )
                            except Exception as tracking_error:
                                # Don't fail schedule import if tracking fails
                                logger.warning(
                                    "job_template_schedule_tracking_failed",
                                    source_id=source_schedule_id,
                                    target_id=result.get("id"),
                                    schedule_name=schedule_name,
                                    error=str(tracking_error),
                                )
                    except Exception as e:
                        logger.error(
                            "job_template_schedule_import_failed",
                            template_id=template_id,
                            template_name=template_name,
                            schedule_name=schedule_name,
                            error=str(e),
                        )

        # Associate notification templates
        if templates_with_notifications:
            logger.info(
                "associating_job_template_notifications",
                total_templates_with_notifications=len(templates_with_notifications),
            )

            # Track notification association warnings for migration report
            notification_warnings: dict[
                Any, list[str]
            ] = {}  # template_id -> list of warning messages

            for notif_data in templates_with_notifications:
                template_id = notif_data["template_id"]
                template_name = notif_data["template_name"]
                source_template_id = notif_data.get("source_template_id")
                notifications = notif_data["notifications"]

                for notif_type, source_notif_ids in notifications.items():
                    for source_notif_id in source_notif_ids:
                        # Map notification template ID from source to target
                        target_notif_id = self.state.get_mapped_id(
                            "notification_templates", source_notif_id
                        )

                        if not target_notif_id:
                            warning_msg = f"Notification template (source ID: {source_notif_id}) not migrated - {notif_type} notification not associated"
                            logger.warning(
                                "notification_template_not_migrated",
                                template_id=template_id,
                                template_name=template_name,
                                source_notif_id=source_notif_id,
                                notif_type=notif_type,
                            )
                            # Track warning for this template
                            if source_template_id:
                                if source_template_id not in notification_warnings:
                                    notification_warnings[source_template_id] = []
                                notification_warnings[source_template_id].append(warning_msg)
                            continue

                        try:
                            await self.client.post(
                                f"job_templates/{template_id}/{notif_type}/",
                                json_data={"id": target_notif_id},
                            )
                            logger.info(
                                "job_template_notification_associated",
                                template_id=template_id,
                                template_name=template_name,
                                notification_id=target_notif_id,
                                notif_type=notif_type,
                            )
                        except Exception as e:
                            warning_msg = f"Failed to associate {notif_type} notification: {str(e)}"
                            logger.error(
                                "job_template_notification_association_failed",
                                template_id=template_id,
                                template_name=template_name,
                                notification_id=target_notif_id,
                                notif_type=notif_type,
                                error=str(e),
                            )
                            # Track warning for this template
                            if source_template_id:
                                if source_template_id not in notification_warnings:
                                    notification_warnings[source_template_id] = []
                                notification_warnings[source_template_id].append(warning_msg)

            # Update database with warnings for templates with incomplete notification associations
            if notification_warnings:
                self._add_notification_warnings("job_templates", notification_warnings)

        return results

    async def _associate_credentials(
        self,
        job_template_id: int,
        credentials: list[dict[str, Any]],
        template_name: str | None = None,
    ) -> None:
        """Associate credentials with a job template via POST.

        Args:
            job_template_id: Target job template ID
            credentials: List of credential dictionaries (containing 'id') to associate
            template_name: Job template name for logging
        """
        endpoint = f"job_templates/{job_template_id}/credentials/"

        for cred_data in credentials:
            # Extract source ID from credential data
            source_cred_id = cred_data.get("id")
            if not source_cred_id:
                continue

            # Resolve Source ID to Target ID
            target_cred_id = self.state.get_mapped_id("credentials", source_cred_id)

            if not target_cred_id:
                logger.warning(
                    "credential_mapping_missing_for_association",
                    job_template_id=job_template_id,
                    source_credential_id=source_cred_id,
                    template_name=template_name,
                    message="Skipping association - credential not found in map",
                )
                continue

            try:
                await self.client.post(endpoint, json_data={"id": target_cred_id})
                logger.debug(
                    "credential_associated_with_job_template",
                    job_template_id=job_template_id,
                    credential_id=target_cred_id,
                    source_credential_id=source_cred_id,
                    template_name=template_name,
                )
            except Exception as e:
                logger.error(
                    "failed_to_associate_credential",
                    job_template_id=job_template_id,
                    credential_id=target_cred_id,
                    template_name=template_name,
                    error=str(e),
                )
