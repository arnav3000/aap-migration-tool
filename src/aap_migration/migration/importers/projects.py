import asyncio
from collections.abc import Callable
from typing import Any

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class ProjectImporter(ResourceImporter):
    """Importer for project resources."""

    DEPENDENCIES: dict[str, str] = {
        "organization": "organizations",
        "credential": "credentials",
        "default_environment": "execution_environments",
        "signature_validation_credential": "credentials",
    }

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Import a project, patching SCM credential onto existing targets when needed.

        Name prefixes do not affect credential FKs (those are ID-mapped). Re-runs
        often find the project already created without a credential (e.g. creds
        were skipped earlier) — still attach the mapped credential.
        """
        working = dict(data)
        if resolve_dependencies:
            working = await self._resolve_dependencies(resource_type, working)

        target_id = self.state.get_mapped_id(resource_type, source_id)
        if self.state.is_migrated(resource_type, source_id) and target_id is not None:
            patched = await self._ensure_project_credential(int(target_id), working, source_id)
            self.stats["skipped_count"] += 1
            return {
                "id": int(target_id),
                "name": working.get("name") or "unknown",
                "_already_migrated": True,
                "_skip_reason": (
                    f"Already migrated (target id {target_id})"
                    + ("; SCM credential attached" if patched else "")
                ),
            }

        result = await super().import_resource(
            resource_type,
            source_id,
            working,
            resolve_dependencies=False,
        )
        if result and result.get("id"):
            await self._ensure_project_credential(int(result["id"]), working, source_id)
        return result

    async def _ensure_project_credential(
        self,
        target_id: int,
        data: dict[str, Any],
        source_id: int,
    ) -> bool:
        """PATCH ``credential`` onto the target project when missing/mismatched.

        Returns True when an update was applied.
        """
        credential_id = data.get("credential")
        if not credential_id:
            return False

        try:
            current = await self.client.get(f"projects/{target_id}/")
            if not isinstance(current, dict):
                return False
            if current.get("credential") == credential_id:
                return False

            await self.client.update_resource(
                "projects",
                target_id,
                {"credential": credential_id},
            )
            logger.info(
                "project_credential_attached",
                source_id=source_id,
                target_id=target_id,
                credential_id=credential_id,
                previous_credential=current.get("credential"),
                project_name=data.get("name"),
            )
            return True
        except Exception as exc:
            logger.warning(
                "project_credential_attach_failed",
                source_id=source_id,
                target_id=target_id,
                credential_id=credential_id,
                error=str(exc),
            )
            return False

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
        # Extract schedules before import
        projects_with_schedules = []
        for project in projects:
            schedules = project.pop("schedules", None)
            if schedules:
                source_id = project.get("_source_id", project.get("id"))
                projects_with_schedules.append(
                    {
                        "source_project_id": source_id,
                        "schedules": schedules,
                    }
                )

        # Import projects
        results = await self._import_parallel("projects", projects, progress_callback)

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
                        sched_tgt_id = result.get("id")
                        if source_schedule_id and sched_tgt_id is not None:
                            try:
                                self.state.save_id_mapping(
                                    resource_type="schedules",
                                    source_id=int(source_schedule_id),
                                    target_id=int(sched_tgt_id),
                                    source_name=schedule_name,
                                    target_name=schedule_name,
                                )
                                self.state.mark_completed(
                                    resource_type="schedules",
                                    source_id=int(source_schedule_id),
                                    target_id=int(sched_tgt_id),
                                    target_name=schedule_name,
                                    source_name=schedule_name,
                                )
                                logger.debug(
                                    "project_schedule_tracked",
                                    source_id=source_schedule_id,
                                    target_id=int(sched_tgt_id),
                                    schedule_name=schedule_name,
                                )
                            except Exception as tracking_error:
                                # Don't fail schedule import if tracking fails
                                logger.warning(
                                    "project_schedule_tracking_failed",
                                    source_id=source_schedule_id,
                                    target_id=int(sched_tgt_id),
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
