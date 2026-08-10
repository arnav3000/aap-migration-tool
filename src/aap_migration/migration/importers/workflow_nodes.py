from collections.abc import Callable
from typing import Any

from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class WorkflowNodeImporter(ResourceImporter):
    """Importer for workflow node resources.

    Workflow nodes form a directed graph with edges. Nodes depend on:
    - workflow_job_template (required)
    - unified_job_template (optional, for non-approval nodes)

    Edge relationships (success_nodes, failure_nodes, always_nodes) are
    removed during initial import and should be handled separately.

    NOTE: Workflow nodes use a nested endpoint under workflow_job_templates,
    not the flat /workflow_nodes/ endpoint.
    """

    DEPENDENCIES: dict[str, str] = {
        "workflow_job_template": "workflow_job_templates",
        "unified_job_template": "unified_job_templates",
    }

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Override to use nested workflow node endpoint.

        Workflow nodes must be created at:
        /workflow_job_templates/{workflow_id}/workflow_nodes/
        not at /workflow_nodes/
        """
        # Get the workflow template ID (should be target ID, not source)
        workflow_target_id = data.get("workflow_job_template")
        if not workflow_target_id:
            logger.error(
                "workflow_node_missing_workflow_id",
                source_id=source_id,
                data_keys=list(data.keys()),
            )
            return None

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
            source_name=data.get("identifier", "unknown"),
            phase="import",
        )

        try:
            # Resolve unified_job_template dependency only
            # (workflow_job_template is already the target ID)
            resolved = dict(data)
            if "unified_job_template" in resolved and resolved["unified_job_template"]:
                ujt_source_id = resolved["unified_job_template"]
                # Try to map the unified job template
                # This could be a job_template, workflow_job_template, or other template type
                # For now, assume it's a job_template (most common case)
                target_id = self.state.get_mapped_id("job_templates", ujt_source_id)
                if target_id:
                    resolved["unified_job_template"] = target_id
                else:
                    # SECURITY FIX: Fail import if referenced job template is missing
                    # Creating a node without its job template creates a broken/incomplete workflow
                    error_msg = (
                        f"Cannot import workflow node: Referenced job template "
                        f"(source_id={ujt_source_id}) was not successfully imported. "
                        f"Ensure all job templates are imported before importing workflows."
                    )

                    logger.error(
                        "workflow_node_dependency_missing",
                        source_id=source_id,
                        ujt_source_id=ujt_source_id,
                        node_name=data.get("identifier", "unknown"),
                        error=error_msg,
                    )

                    # Mark as failed in database
                    self.stats["error_count"] += 1
                    self.state.mark_failed(
                        resource_type=resource_type,
                        source_id=source_id,
                        error_message=error_msg,
                    )

                    # Track for reporting
                    self.import_errors.append(
                        {
                            "resource_type": resource_type,
                            "source_id": source_id,
                            "name": data.get("identifier", "unknown"),
                            "error": error_msg,
                            "error_type": "DependencyError",
                        }
                    )

                    # Return None to stop processing this broken node
                    return None

            # Keep workflow_job_template in data (it's required for POST even though it's in the URL)
            # Just remove the source workflow ID tracking field
            resolved.pop("_source_workflow_id", None)

            # Extract edge fields before removing (will be handled after all nodes exist)
            edge_data = {
                "success_nodes": data.get("success_nodes", []),
                "failure_nodes": data.get("failure_nodes", []),
                "always_nodes": data.get("always_nodes", []),
            }
            resolved.pop("success_nodes", None)
            resolved.pop("failure_nodes", None)
            resolved.pop("always_nodes", None)

            # Remove read-only/metadata fields that shouldn't be in POST
            read_only_fields = [
                "id",
                "type",
                "url",
                "related",
                "summary_fields",
                "created",
                "modified",
                "natural_key",
            ]
            for field in read_only_fields:
                resolved.pop(field, None)

            # Remove None values
            resolved = {k: v for k, v in resolved.items() if v is not None}

            # Use nested endpoint
            nested_endpoint = f"workflow_job_templates/{workflow_target_id}/workflow_nodes/"

            # Log the data being sent for debugging
            logger.debug(
                "workflow_node_create_attempt",
                endpoint=nested_endpoint,
                data_keys=list(resolved.keys()),
                data=resolved,
            )

            # Create the node using the nested endpoint (use json_data parameter)
            result = await self.client.post(nested_endpoint, json_data=resolved)

            # Mark as completed
            self.state.mark_completed(
                resource_type=resource_type,
                source_id=source_id,
                target_id=result["id"],
                target_name=result.get("identifier", "unknown"),
            )

            self.stats["imported_count"] += 1

            logger.info(
                "workflow_node_imported",
                source_id=source_id,
                target_id=result["id"],
                workflow_id=workflow_target_id,
            )

            # Attach edge data and source ID to result for later edge creation
            result["_edge_data"] = edge_data
            result["_source_id"] = source_id

            return result

        except Exception as e:
            logger.error(
                "workflow_node_import_failed",
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
                    "name": data.get("identifier", "unknown"),
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
            )

            return None

    async def import_workflow_nodes(
        self,
        nodes: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple workflow nodes.

        Handles workflow and template dependency resolution.
        Edge relationships are removed before import (handled separately).

        Args:
            nodes: List of workflow node data
            progress_callback: Optional callback for progress updates.
                Called after each node with (success_count, failed_count).

        Returns:
            List of created workflow node data
        """
        results = []
        success_count = 0
        failed_count = 0

        for node in nodes:
            source_id = node.pop("_source_id", node.get("id"))

            # Don't remove edge fields here - import_resource() will extract and store them
            # The edge creation happens after all nodes are imported

            try:
                result = await self.import_resource(
                    resource_type="workflow_nodes",
                    source_id=source_id,
                    data=node,
                )
                if result:
                    results.append(result)
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                failed_count += 1

                # Mark as failed in database
                self.state.mark_failed(
                    resource_type="workflow_nodes",
                    source_id=source_id,
                    error_message=f"{type(e).__name__}: {str(e)}",
                )

                # Log the error
                logger.error(
                    "workflow_node_import_failed",
                    resource_type="workflow_nodes",
                    source_id=source_id,
                    node_name=node.get("identifier", "unknown"),
                    error=str(e),
                )

                # Track error for reporting
                self.import_errors.append(
                    {
                        "resource_type": "workflow_nodes",
                        "source_id": source_id,
                        "name": node.get("identifier", "unknown"),
                        "error": str(e),
                        "error_type": type(e).__name__,
                    }
                )

                raise
            finally:
                # Update progress after each node
                if progress_callback:
                    progress_callback(
                        self.stats["imported_count"],
                        self.stats["error_count"],
                        self.stats["skipped_count"],
                    )

        return results
