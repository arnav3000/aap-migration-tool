from collections.abc import Callable
from typing import Any

from aap_migration.migration.database import get_session
from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.migration.importers.workflow_nodes import WorkflowNodeImporter
from aap_migration.migration.models import MigrationProgress
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class WorkflowImporter(ResourceImporter):
    """Importer for workflow job template resources."""

    DEPENDENCIES: dict[str, str] = {
        "organization": "organizations",
        "inventory": "inventories",
        "webhook_credential": "credentials",
    }

    @staticmethod
    def _workflow_node_failure_message(
        node_errors: list[dict[str, Any]],
        imported_count: int,
        expected_count: int,
    ) -> str:
        """Build a workflow-level error message from failed node imports."""
        if node_errors:
            details = "; ".join(
                f"{err.get('name', 'unknown')}: {err.get('error', 'unknown error')}"
                for err in node_errors
            )
            return (
                f"Workflow node import failed ({imported_count}/{expected_count} created). "
                f"{details}"
            )
        return (
            f"Workflow node import failed: only {imported_count} of {expected_count} "
            f"nodes were created."
        )

    def _finalize_pending_workflow(
        self,
        source_id: int,
        info: dict[str, Any],
        node_errors: list[dict[str, Any]],
        imported_count: int,
        expected_count: int,
        results: list[dict[str, Any]],
        workflows_with_surveys: list[dict[str, Any]],
        workflows_with_schedules: list[dict[str, Any]],
        workflows_with_notifications: list[dict[str, Any]],
    ) -> bool:
        """Mark workflow failed or append to success results after node import.

        Returns True if the workflow succeeded, False if it failed.
        """
        result = info["result"]
        workflow_name = info.get("name") or result.get("name", "unknown")

        if imported_count < expected_count or node_errors:
            error_msg = self._workflow_node_failure_message(
                node_errors, imported_count, expected_count
            )
            self.state.mark_failed(
                resource_type="workflow_job_templates",
                source_id=source_id,
                error_message=error_msg,
            )
            self._record_import_failure(
                "workflow_job_templates",
                source_id,
                str(workflow_name),
                error_msg,
                error_type="WorkflowNodeImportError",
            )
            logger.error(
                "workflow_import_failed_due_to_nodes",
                workflow_name=workflow_name,
                source_workflow_id=source_id,
                imported_count=imported_count,
                expected_count=expected_count,
                error=error_msg,
            )
            return False

        results.append(result)
        if info.get("survey_spec"):
            workflows_with_surveys.append(
                {
                    "workflow_id": result["id"],
                    "workflow_name": workflow_name,
                    "survey_spec": info["survey_spec"],
                }
            )
        if info.get("schedules"):
            workflows_with_schedules.append(
                {
                    "source_workflow_id": source_id,
                    "workflow_id": result["id"],
                    "workflow_name": workflow_name,
                    "schedules": info["schedules"],
                }
            )
        if info.get("notifications"):
            workflows_with_notifications.append(
                {
                    "source_workflow_id": source_id,
                    "workflow_id": result["id"],
                    "workflow_name": workflow_name,
                    "notifications": info["notifications"],
                }
            )
        return True

    async def import_workflows(
        self,
        workflows: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple workflow job templates with live progress updates.

        Imports workflows first, then automatically imports their workflow nodes.
        Workflows use sequential import (not parallel) to properly track node metadata.

        Args:
            workflows: List of workflow data
            progress_callback: Optional callback for progress updates.
                Called after each workflow with (success_count, failed_count).

        Returns:
            List of created workflow data
        """
        results = []
        success_count = 0
        failed_count = 0
        skipped_count = 0
        all_pending_nodes = []  # Collect all nodes for batch import
        pending_workflows: dict[Any, dict[str, Any]] = {}  # Defer success until nodes import
        node_source_to_workflow: dict[int, Any] = {}  # Map node source id -> workflow source id
        workflows_with_surveys = []  # Collect workflows that have surveys to apply
        workflows_with_schedules = []  # Collect workflows that have schedules to create
        workflows_with_notifications = []  # Collect workflows that have notification associations

        # Query failed dependencies once (efficient approach)
        # This allows us to check if workflow nodes reference failed templates
        failed_job_template_ids = set()
        failed_workflow_template_ids = set()

        try:
            with get_session(self.state.database_url) as session:
                # Get all failed job templates
                failed_jobs = (
                    session.query(MigrationProgress.source_id)
                    .filter(
                        MigrationProgress.resource_type == "job_templates",
                        MigrationProgress.status == "failed",
                    )
                    .all()
                )
                failed_job_template_ids = {row.source_id for row in failed_jobs}

                # Get all failed workflow templates
                failed_workflows = (
                    session.query(MigrationProgress.source_id)
                    .filter(
                        MigrationProgress.resource_type == "workflow_job_templates",
                        MigrationProgress.status == "failed",
                    )
                    .all()
                )
                failed_workflow_template_ids = {row.source_id for row in failed_workflows}

                logger.info(
                    "Loaded failed dependencies for validation",
                    failed_job_templates=len(failed_job_template_ids),
                    failed_workflow_templates=len(failed_workflow_template_ids),
                )
        except Exception as e:
            logger.warning(
                "Failed to query failed dependencies, will skip validation",
                error=str(e),
            )

        # Phase 1: Import workflows and collect nodes/surveys/schedules/notifications
        for workflow in workflows:
            source_id = workflow.pop("_source_id", workflow.get("id"))

            # Extract nodes for separate import
            nodes = workflow.pop("_workflow_nodes", None)

            # Extract survey spec for separate import (must be POSTed after workflow creation)
            survey_spec = workflow.pop("survey_spec", None)

            # Extract schedules for separate import
            schedules = workflow.pop("schedules", None)

            # Extract notification associations for separate import
            notifications = workflow.pop("notifications", None)

            # SECURITY FIX: Validate all node dependencies BEFORE importing workflow
            # Check if nodes reference any FAILED templates from earlier import phases
            if nodes and (failed_job_template_ids or failed_workflow_template_ids):
                missing_dependencies = []
                for node in nodes:
                    ujt_source_id = node.get("unified_job_template")
                    if ujt_source_id:
                        # Determine the type and name of unified_job_template
                        ujt_summary = node.get("summary_fields", {}).get("unified_job_template", {})
                        ujt_type = ujt_summary.get("unified_job_type")
                        ujt_name = ujt_summary.get("name") or "Unknown"

                        # Check if this node references a FAILED template
                        if ujt_type == "job" and ujt_source_id in failed_job_template_ids:
                            missing_dependencies.append((ujt_source_id, ujt_type, ujt_name))
                        elif (
                            ujt_type == "workflow_job"
                            and ujt_source_id in failed_workflow_template_ids
                        ):
                            missing_dependencies.append((ujt_source_id, ujt_type, ujt_name))
                        elif ujt_type is None or ujt_type not in ["job", "workflow_job"]:
                            # Unknown/missing type - check both sets to be safe
                            # This handles data corruption or unexpected ujt_type values
                            if ujt_source_id in failed_job_template_ids:
                                missing_dependencies.append(
                                    (ujt_source_id, "job (assumed)", ujt_name)
                                )
                            elif ujt_source_id in failed_workflow_template_ids:
                                missing_dependencies.append(
                                    (ujt_source_id, "workflow_job (assumed)", ujt_name)
                                )

                if missing_dependencies:
                    # Don't import this workflow - has failed dependencies
                    failed_count += 1

                    # Deduplicate and count missing dependencies
                    # Key: (source_id, type, name), Value: count of references
                    dep_counts: dict[tuple[Any, Any, Any], int] = {}
                    for source_id_val, dep_type, dep_name in missing_dependencies:
                        key = (source_id_val, dep_type, dep_name)
                        dep_counts[key] = dep_counts.get(key, 0) + 1

                    # Format missing dependencies for error message
                    missing_items = []
                    for (source_id_val, dep_type, dep_name), count in dep_counts.items():
                        if count > 1:
                            missing_items.append(
                                f"'{dep_name}' ({dep_type} template, ID: {source_id_val}) [referenced by {count} nodes]"
                            )
                        else:
                            missing_items.append(
                                f"'{dep_name}' ({dep_type} template, ID: {source_id_val})"
                            )

                    error_msg = (
                        f"Cannot import workflow: {len(dep_counts)} unique template(s) "
                        f"failed to import in earlier phases. Missing: {', '.join(missing_items)}. "
                        f"Fix the failed templates first, then retry workflow import."
                    )

                    # Create progress record only when needed (before marking failed)
                    # This avoids double-call with import_resource() for successful imports
                    self.state.mark_in_progress(
                        resource_type="workflow_job_templates",
                        source_id=source_id,
                        source_name=str(workflow.get("name") or ""),
                        phase="import",
                    )

                    # Now mark as failed (record exists from mark_in_progress above)
                    self.state.mark_failed(
                        resource_type="workflow_job_templates",
                        source_id=source_id,
                        error_message=error_msg,
                    )

                    self._record_import_failure(
                        "workflow_job_templates",
                        source_id,
                        str(workflow.get("name") or "unknown"),
                        error_msg,
                    )

                    logger.error(
                        "workflow_dependency_check_failed",
                        workflow_source_id=source_id,
                        workflow_name=workflow.get("name"),
                        missing_dependencies=missing_dependencies,
                        error=error_msg,
                    )

                    # Update progress after failure
                    if progress_callback:
                        progress_callback(success_count, failed_count, skipped_count)

                    continue  # Skip to next workflow

            try:
                result = await self.import_resource(
                    resource_type="workflow_job_templates",
                    source_id=source_id,
                    data=workflow,
                )
            except Exception as e:
                failed_count += 1

                # Mark as failed in database
                self.state.mark_failed(
                    resource_type="workflow_job_templates",
                    source_id=source_id,
                    error_message=f"{type(e).__name__}: {str(e)}",
                )

                self._record_import_failure(
                    "workflow_job_templates",
                    source_id,
                    str(workflow.get("name") or "unknown"),
                    f"{type(e).__name__}: {str(e)}",
                    error_type=type(e).__name__,
                )

                logger.error(
                    "workflow_import_failed",
                    source_id=source_id,
                    name=workflow.get("name"),
                    error=str(e),
                )

                # Update progress after failure
                if progress_callback:
                    progress_callback(success_count, failed_count, skipped_count)

                continue  # Skip to next workflow

            if result:
                result["_source_id"] = source_id

                if nodes and len(nodes) > 0:
                    # Defer workflow success until all nodes are imported
                    for node in nodes:
                        node["workflow_job_template"] = result["id"]
                        node["_source_workflow_id"] = source_id
                        node_source_id = node.get("_source_id") or node.get("id")
                        if node_source_id is not None:
                            node_source_to_workflow[int(node_source_id)] = source_id
                    all_pending_nodes.extend(nodes)
                    pending_workflows[source_id] = {
                        "result": result,
                        "name": workflow.get("name", "unknown"),
                        "node_count": len(nodes),
                        "survey_spec": survey_spec,
                        "schedules": schedules,
                        "notifications": notifications,
                    }
                else:
                    # No nodes to import — shell-only workflow is complete
                    results.append(result)
                    success_count += 1

                    if survey_spec:
                        workflows_with_surveys.append(
                            {
                                "workflow_id": result["id"],
                                "workflow_name": result.get("name", "unknown"),
                                "survey_spec": survey_spec,
                            }
                        )

                    if schedules:
                        workflows_with_schedules.append(
                            {
                                "source_workflow_id": source_id,
                                "workflow_id": result["id"],
                                "workflow_name": result.get("name", "unknown"),
                                "schedules": schedules,
                            }
                        )

                    if notifications:
                        workflows_with_notifications.append(
                            {
                                "source_workflow_id": source_id,
                                "workflow_id": result["id"],
                                "workflow_name": result.get("name", "unknown"),
                                "notifications": notifications,
                            }
                        )
            else:
                failed_count += 1
                error_detail = self._failure_detail_for_resource(
                    "workflow_job_templates", source_id
                )
                self._record_import_failure(
                    "workflow_job_templates",
                    source_id,
                    str(workflow.get("name") or "unknown"),
                    error_detail,
                )

            # Update progress after each workflow
            if progress_callback:
                progress_callback(success_count, failed_count, skipped_count)

        # Phase 2: Import all workflow nodes
        if all_pending_nodes:
            logger.info(
                "importing_workflow_nodes",
                total_nodes=len(all_pending_nodes),
                total_workflows=len(pending_workflows),
            )

            node_importer = WorkflowNodeImporter(
                client=self.client,
                state=self.state,
                performance_config=self.performance_config,
            )

            try:
                imported_nodes = await node_importer.import_workflow_nodes(
                    all_pending_nodes,
                    progress_callback=None,
                )

                nodes_imported = len(imported_nodes)
                nodes_expected = len(all_pending_nodes)
                nodes_failed = len(node_importer.import_errors)

                logger.info(
                    "workflow_nodes_imported",
                    imported_count=nodes_imported,
                    failed_count=nodes_failed,
                    total_nodes=nodes_expected,
                )

                failed_by_workflow: dict[Any, list[dict[str, Any]]] = {}
                for error_record in node_importer.import_errors:
                    source_workflow_id = error_record.get("source_workflow_id")
                    if source_workflow_id is not None:
                        failed_by_workflow.setdefault(source_workflow_id, []).append(error_record)

                imported_by_workflow: dict[Any, int] = {}
                for node_result in imported_nodes:
                    node_source_id = node_result.get("_source_id")
                    if node_source_id is None:
                        continue
                    workflow_source_id = node_source_to_workflow.get(int(node_source_id))
                    if workflow_source_id is not None:
                        imported_by_workflow[workflow_source_id] = (
                            imported_by_workflow.get(workflow_source_id, 0) + 1
                        )

                successful_workflow_ids: set[Any] = set()
                for source_workflow_id, info in pending_workflows.items():
                    expected_count = info["node_count"]
                    imported_count = imported_by_workflow.get(source_workflow_id, 0)
                    node_errors = failed_by_workflow.get(source_workflow_id, [])

                    if self._finalize_pending_workflow(
                        source_workflow_id,
                        info,
                        node_errors,
                        imported_count,
                        expected_count,
                        results,
                        workflows_with_surveys,
                        workflows_with_schedules,
                        workflows_with_notifications,
                    ):
                        successful_workflow_ids.add(source_workflow_id)
                        success_count += 1
                    else:
                        failed_count += 1

                    if progress_callback:
                        progress_callback(success_count, failed_count, skipped_count)

                # Phase 3: Create edges only for fully successful workflows
                nodes_for_edges = []
                for node in imported_nodes:
                    node_source_id = node.get("_source_id")
                    if node_source_id is None:
                        continue
                    workflow_source_id = node_source_to_workflow.get(int(node_source_id))
                    if workflow_source_id in successful_workflow_ids:
                        nodes_for_edges.append(node)
                if nodes_for_edges:
                    logger.info(
                        "starting_edge_creation_phase",
                        node_count=len(nodes_for_edges),
                    )
                    await self._create_workflow_edges(nodes_for_edges)
                else:
                    logger.warning("no_imported_nodes_for_edge_creation")

            except Exception as e:
                logger.error(
                    "workflow_nodes_import_failed",
                    total_nodes=len(all_pending_nodes),
                    error=str(e),
                )

                error_msg = f"Workflow node import failed with exception: {str(e)}"
                for source_workflow_id, info in pending_workflows.items():
                    workflow_name = info.get("name") or info["result"].get("name", "unknown")
                    self.state.mark_failed(
                        resource_type="workflow_job_templates",
                        source_id=source_workflow_id,
                        error_message=error_msg,
                    )
                    self._record_import_failure(
                        "workflow_job_templates",
                        source_workflow_id,
                        str(workflow_name),
                        error_msg,
                        error_type=type(e).__name__,
                    )
                    failed_count += 1
                    logger.error(
                        "workflow_marked_failed_due_to_exception",
                        workflow_name=workflow_name,
                        source_workflow_id=source_workflow_id,
                        error=error_msg,
                    )

                    if progress_callback:
                        progress_callback(success_count, failed_count, skipped_count)

        # Phase 4: Import survey specs
        if workflows_with_surveys:
            logger.info(
                "importing_workflow_surveys",
                total_surveys=len(workflows_with_surveys),
            )

            for survey_data in workflows_with_surveys:
                workflow_id = survey_data["workflow_id"]
                workflow_name = survey_data["workflow_name"]
                survey_spec = survey_data["survey_spec"]

                try:
                    await self.client.post(
                        f"workflow_job_templates/{workflow_id}/survey_spec/",
                        json_data=survey_spec,
                    )
                    logger.info(
                        "workflow_survey_imported",
                        workflow_id=workflow_id,
                        workflow_name=workflow_name,
                        survey_questions=len(survey_spec.get("spec", [])),
                    )
                except Exception as e:
                    logger.error(
                        "workflow_survey_import_failed",
                        workflow_id=workflow_id,
                        workflow_name=workflow_name,
                        error=str(e),
                    )

        # Phase 5: Import schedules
        if workflows_with_schedules:
            logger.info(
                "importing_workflow_schedules",
                total_workflows_with_schedules=len(workflows_with_schedules),
            )

            for schedule_data in workflows_with_schedules:
                source_workflow_id = schedule_data["source_workflow_id"]
                workflow_id = schedule_data["workflow_id"]
                workflow_name = schedule_data["workflow_name"]
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

                    # SAFETY: Disable schedule by default to prevent automatic execution
                    original_enabled = schedule_to_import.get("enabled", True)
                    schedule_to_import["enabled"] = False

                    try:
                        result = await self.client.post(
                            f"workflow_job_templates/{workflow_id}/schedules/",
                            json_data=schedule_to_import,
                        )
                        logger.info(
                            "workflow_schedule_imported",
                            workflow_id=workflow_id,
                            workflow_name=workflow_name,
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
                                    "workflow_schedule_tracked",
                                    source_id=source_schedule_id,
                                    target_id=int(sched_tgt_id),
                                    schedule_name=schedule_name,
                                )
                            except Exception as tracking_error:
                                # Don't fail schedule import if tracking fails
                                logger.warning(
                                    "workflow_schedule_tracking_failed",
                                    source_id=source_schedule_id,
                                    target_id=int(sched_tgt_id),
                                    schedule_name=schedule_name,
                                    error=str(tracking_error),
                                )
                    except Exception as e:
                        logger.error(
                            "workflow_schedule_import_failed",
                            workflow_id=workflow_id,
                            workflow_name=workflow_name,
                            schedule_name=schedule_name,
                            error=str(e),
                        )

        # Phase 6: Associate notification templates
        if workflows_with_notifications:
            logger.info(
                "associating_workflow_notifications",
                total_workflows_with_notifications=len(workflows_with_notifications),
            )

            # Track notification association warnings for migration report
            notification_warnings: dict[
                Any, list[str]
            ] = {}  # workflow_id -> list of warning messages

            for notif_data in workflows_with_notifications:
                workflow_id = notif_data["workflow_id"]
                workflow_name = notif_data["workflow_name"]
                source_workflow_id = notif_data.get("source_workflow_id")
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
                                workflow_id=workflow_id,
                                workflow_name=workflow_name,
                                source_notif_id=source_notif_id,
                                notif_type=notif_type,
                            )
                            # Track warning for this workflow
                            if source_workflow_id:
                                if source_workflow_id not in notification_warnings:
                                    notification_warnings[source_workflow_id] = []
                                notification_warnings[source_workflow_id].append(warning_msg)
                            continue

                        try:
                            await self.client.post(
                                f"workflow_job_templates/{workflow_id}/{notif_type}/",
                                json_data={"id": target_notif_id},
                            )
                            logger.info(
                                "workflow_notification_associated",
                                workflow_id=workflow_id,
                                workflow_name=workflow_name,
                                notification_id=target_notif_id,
                                notif_type=notif_type,
                            )
                        except Exception as e:
                            warning_msg = f"Failed to associate {notif_type} notification: {str(e)}"
                            logger.error(
                                "workflow_notification_association_failed",
                                workflow_id=workflow_id,
                                workflow_name=workflow_name,
                                notification_id=target_notif_id,
                                notif_type=notif_type,
                                error=str(e),
                            )
                            # Track warning for this workflow
                            if source_workflow_id:
                                if source_workflow_id not in notification_warnings:
                                    notification_warnings[source_workflow_id] = []
                                notification_warnings[source_workflow_id].append(warning_msg)

            # Update database with warnings for workflows with incomplete notification associations
            if notification_warnings:
                self._add_notification_warnings("workflow_job_templates", notification_warnings)

        return results

    async def _create_workflow_edges(self, nodes: list[dict[str, Any]]) -> None:
        """Create edges (connections) between workflow nodes.

        Must be called after all nodes are imported so we can map source IDs to target IDs.

        Args:
            nodes: List of imported node data with _edge_data and _source_id attached
        """
        # Build mapping of source node ID -> target node ID
        node_id_map = {}
        for node in nodes:
            source_id = node.get("_source_id")
            target_id = node.get("id")
            if source_id and target_id:
                node_id_map[source_id] = target_id

        logger.info(
            "creating_workflow_edges",
            total_nodes=len(nodes),
            node_id_mappings=len(node_id_map),
        )

        edge_count = 0
        failed_edges = 0

        for node in nodes:
            target_node_id = node.get("id")
            edge_data = node.get("_edge_data", {})

            if not target_node_id or not edge_data:
                continue

            # Create success edges
            for source_child_id in edge_data.get("success_nodes", []):
                target_child_id = node_id_map.get(source_child_id)
                if target_child_id:
                    try:
                        await self.client.post(
                            f"workflow_job_template_nodes/{target_node_id}/success_nodes/",
                            json_data={"id": target_child_id},
                        )
                        edge_count += 1
                        logger.debug(
                            "workflow_edge_created",
                            edge_type="success",
                            from_node=target_node_id,
                            to_node=target_child_id,
                        )
                    except Exception as e:
                        failed_edges += 1
                        logger.warning(
                            "workflow_edge_failed",
                            edge_type="success",
                            from_node=target_node_id,
                            to_node=target_child_id,
                            error=str(e),
                        )

            # Create failure edges
            for source_child_id in edge_data.get("failure_nodes", []):
                target_child_id = node_id_map.get(source_child_id)
                if target_child_id:
                    try:
                        await self.client.post(
                            f"workflow_job_template_nodes/{target_node_id}/failure_nodes/",
                            json_data={"id": target_child_id},
                        )
                        edge_count += 1
                        logger.debug(
                            "workflow_edge_created",
                            edge_type="failure",
                            from_node=target_node_id,
                            to_node=target_child_id,
                        )
                    except Exception as e:
                        failed_edges += 1
                        logger.warning(
                            "workflow_edge_failed",
                            edge_type="failure",
                            from_node=target_node_id,
                            to_node=target_child_id,
                            error=str(e),
                        )

            # Create always edges
            for source_child_id in edge_data.get("always_nodes", []):
                target_child_id = node_id_map.get(source_child_id)
                if target_child_id:
                    try:
                        await self.client.post(
                            f"workflow_job_template_nodes/{target_node_id}/always_nodes/",
                            json_data={"id": target_child_id},
                        )
                        edge_count += 1
                        logger.debug(
                            "workflow_edge_created",
                            edge_type="always",
                            from_node=target_node_id,
                            to_node=target_child_id,
                        )
                    except Exception as e:
                        failed_edges += 1
                        logger.warning(
                            "workflow_edge_failed",
                            edge_type="always",
                            from_node=target_node_id,
                            to_node=target_child_id,
                            error=str(e),
                        )

        logger.info(
            "workflow_edges_created",
            total_edges=edge_count,
            failed_edges=failed_edges,
        )

    async def import_workflow_job_templates(
        self,
        workflows: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Alias for import_workflows to match CLI method naming convention."""
        return await self.import_workflows(workflows, progress_callback)
