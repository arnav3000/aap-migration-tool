"""Domain resource importers (split from migration.importer; re-exported)."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aap_migration.migration.importers.base import (
    ResourceImporter,
    logger,
)


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

    DEPENDENCIES = {
        "workflow_job_template": "workflow_job_templates",
        "unified_job_template": "unified_job_templates",
        "inventory": "inventories",
        "execution_environment": "execution_environments",
    }

    _FIELD_TO_LAUNCH_FLAG = {
        "inventory": "ask_inventory_on_launch",
        "extra_data": "ask_variables_on_launch",
        "limit": "ask_limit_on_launch",
        "diff_mode": "ask_diff_mode_on_launch",
        "job_tags": "ask_tags_on_launch",
        "skip_tags": "ask_skip_tags_on_launch",
        "verbosity": "ask_verbosity_on_launch",
        "job_type": "ask_job_type_on_launch",
        "scm_branch": "ask_scm_branch_on_launch",
        "forks": "ask_forks_on_launch",
        "timeout": "ask_timeout_on_launch",
        "job_slice_count": "ask_job_slice_count_on_launch",
    }

    def __init__(self, *args: Any, input_dir: Path | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.input_dir = input_dir
        self._template_launch_config_cache: dict[tuple[str, int], dict] | None = None

    def _load_template_launch_config(self) -> dict[tuple[str, int], dict]:
        if self._template_launch_config_cache is not None:
            return self._template_launch_config_cache

        cache: dict[tuple[str, int], dict] = {}
        self._template_launch_config_cache = cache

        if not self.input_dir:
            return cache

        for resource_type in ("job_templates", "workflow_job_templates"):
            template_dir = self.input_dir / resource_type
            if not template_dir.exists():
                continue

            for json_file in sorted(template_dir.glob(f"{resource_type}_*.json")):
                try:
                    with open(json_file) as f:
                        templates = json.load(f)

                    if not isinstance(templates, list):
                        templates = [templates]

                    for template in templates:
                        source_id = template.get("_source_id") or template.get("id")
                        if not source_id:
                            continue

                        launch_config: dict[str, Any] = {}
                        for key, value in template.items():
                            if key.startswith("ask_") and key.endswith("_on_launch"):
                                launch_config[key] = bool(value)

                        launch_config["survey_enabled"] = bool(
                            template.get("survey_enabled", False)
                        )

                        survey_spec = template.get("survey_spec")
                        if survey_spec and isinstance(survey_spec, dict) and "spec" in survey_spec:
                            survey_vars = set()
                            for question in survey_spec["spec"]:
                                if isinstance(question, dict) and "variable" in question:
                                    survey_vars.add(question["variable"])
                            if survey_vars:
                                launch_config["_survey_vars"] = survey_vars
                            launch_config["_survey_spec"] = survey_spec["spec"]

                        if launch_config:
                            cache[(resource_type, int(source_id))] = launch_config

                except Exception as e:
                    logger.warning(
                        "workflow_node_launch_config_load_error",
                        file=str(json_file),
                        error=str(e),
                    )

        logger.info(
            "workflow_node_launch_config_loaded",
            job_templates=sum(1 for k in cache if k[0] == "job_templates"),
            workflow_templates=sum(1 for k in cache if k[0] == "workflow_job_templates"),
            total=len(cache),
        )

        return cache

    def _get_template_launch_config(self, ujt_type: str, ujt_id: int) -> dict | None:
        cache = self._load_template_launch_config()
        return cache.get((ujt_type, int(ujt_id)))

    def _sanitize_node_extra_data(
        self,
        node_data: dict[str, Any],
        ujt_type: str,
        ujt_source_id: int,
    ) -> dict[str, Any]:
        """Log workflow node overrides that may conflict with template launch config.

        Data is preserved as-is and passed through to AAP 2.6. If the target
        API rejects an override, the failure is captured in the migration report.
        """
        if ujt_type not in ("job_templates", "workflow_job_templates"):
            return node_data

        config = self._get_template_launch_config(ujt_type, ujt_source_id)
        if config is None:
            return node_data

        source_id = node_data.get("_source_id") or node_data.get("id")

        for field, launch_flag in self._FIELD_TO_LAUNCH_FLAG.items():
            if field not in node_data:
                continue
            value = node_data[field]
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, dict) and not value:
                continue

            if not config.get(launch_flag, False):
                logger.info(
                    f"workflow_node_{field}_override_preserved",
                    source_id=source_id,
                    field=field,
                    launch_flag=launch_flag,
                    ujt_type=ujt_type,
                    ujt_id=ujt_source_id,
                    message=(
                        f"Preserving '{field}' override as-is — template does not "
                        f"have '{launch_flag}' enabled. If AAP 2.6 rejects this, "
                        f"the failure will appear in the migration report."
                    ),
                )

        return node_data

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

        # Dispatch specialised node types to dedicated handlers.
        # This keeps all new node-type logic isolated in new methods and leaves
        # the existing UJT resolution path below completely untouched.
        ujt_unified_type = (
            (data.get("summary_fields") or {})
            .get("unified_job_template", {})
            .get("unified_job_type")
        )
        if ujt_unified_type == "workflow_approval":
            return await self._handle_approval_node(source_id, data, workflow_target_id)
        if ujt_unified_type == "system_job":
            return await self._handle_system_job_node(source_id, data, workflow_target_id)

        try:
            # Resolve unified_job_template dependency only
            # (workflow_job_template is already the target ID)
            resolved = dict(data)
            ujt_source_id = None
            ujt_type = None
            if "unified_job_template" in resolved and resolved["unified_job_template"]:
                ujt_source_id = resolved["unified_job_template"]
                # Try to map the unified job template
                # This could be a job_template, workflow_job_template, project, or inventory_source
                # Try each type in order of likelihood
                target_id = None
                ujt_type = None

                # Try job_templates first (most common)
                target_id = self.state.get_mapped_id("job_templates", ujt_source_id)
                if target_id:
                    ujt_type = "job_templates"
                else:
                    # Try workflow_job_templates (nested workflows)
                    target_id = self.state.get_mapped_id("workflow_job_templates", ujt_source_id)
                    if target_id:
                        ujt_type = "workflow_job_templates"
                    else:
                        # Try projects (project sync)
                        target_id = self.state.get_mapped_id("projects", ujt_source_id)
                        if target_id:
                            ujt_type = "projects"
                        else:
                            # Try inventory_sources (inventory sync)
                            target_id = self.state.get_mapped_id("inventory_sources", ujt_source_id)
                            if target_id:
                                ujt_type = "inventory_sources"

                if target_id:
                    resolved["unified_job_template"] = target_id
                    logger.debug(
                        "workflow_node_ujt_resolved",
                        source_id=source_id,
                        ujt_source_id=ujt_source_id,
                        ujt_target_id=target_id,
                        ujt_type=ujt_type,
                    )
                else:
                    # SECURITY FIX: Fail import if referenced template is missing
                    # Creating a node without its template creates a broken/incomplete workflow
                    error_msg = (
                        f"Cannot import workflow node: Referenced unified_job_template "
                        f"(source_id={ujt_source_id}) was not successfully imported. "
                        f"Tried job_templates, workflow_job_templates, projects, and inventory_sources. "
                        f"Ensure all dependencies are imported before importing workflows."
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

            # Resolve inventory FK (optional override on workflow node)
            if "inventory" in resolved and resolved["inventory"]:
                inv_source_id = resolved["inventory"]
                inv_target_id = self.state.get_mapped_id("inventories", inv_source_id)
                if inv_target_id:
                    resolved["inventory"] = inv_target_id
                    logger.debug(
                        "workflow_node_inventory_resolved",
                        source_id=source_id,
                        inv_source_id=inv_source_id,
                        inv_target_id=inv_target_id,
                    )
                else:
                    logger.warning(
                        "workflow_node_inventory_unresolved",
                        source_id=source_id,
                        inv_source_id=inv_source_id,
                    )
                    # Remove unresolved inventory to allow partial import
                    resolved.pop("inventory", None)

            # Resolve execution_environment FK (optional override on workflow node)
            if "execution_environment" in resolved and resolved["execution_environment"]:
                ee_source_id = resolved["execution_environment"]
                ee_target_id = self.state.get_mapped_id("execution_environments", ee_source_id)
                if ee_target_id:
                    resolved["execution_environment"] = ee_target_id
                    logger.debug(
                        "workflow_node_ee_resolved",
                        source_id=source_id,
                        ee_source_id=ee_source_id,
                        ee_target_id=ee_target_id,
                    )
                else:
                    logger.warning(
                        "workflow_node_ee_unresolved",
                        source_id=source_id,
                        ee_source_id=ee_source_id,
                    )
                    # Remove unresolved EE to allow partial import
                    resolved.pop("execution_environment", None)

            # Sanitize extra_data and non-promptable overrides against template launch config
            if ujt_type and ujt_source_id and self.input_dir:
                resolved = self._sanitize_node_extra_data(resolved, ujt_type, ujt_source_id)

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

    # ── Specialised node-type handlers ───────────────────────────────────────
    # These methods handle node types that require a different API flow from the
    # standard UJT resolution path in import_resource().  All new code — the
    # existing import_resource() logic above is untouched.

    async def _handle_approval_node(
        self,
        source_id: int,
        data: dict[str, Any],
        workflow_target_id: int,
    ) -> dict[str, Any] | None:
        """Create an approval workflow node via the two-step AAP API.

        AAP approval nodes cannot be created by setting unified_job_template
        directly.  The correct flow is:
          1. POST node without unified_job_template
          2. POST to create_approval_template to create and link the template
        """
        resource_type = "workflow_nodes"
        ujt_summary = (data.get("summary_fields") or {}).get("unified_job_template") or {}
        approval_name = ujt_summary.get("name") or data.get("identifier", "approval")
        approval_timeout = ujt_summary.get("timeout") or 0
        approval_description = ujt_summary.get("description") or ""

        # Build node payload — omit unified_job_template and read-only fields
        skip_fields = {
            "id",
            "type",
            "url",
            "related",
            "summary_fields",
            "created",
            "modified",
            "natural_key",
            "unified_job_template",
            "success_nodes",
            "failure_nodes",
            "always_nodes",
            "_source_id",
            "_source_workflow_id",
            "_ujt_resource_type",
        }
        resolved = {k: v for k, v in data.items() if k not in skip_fields and v is not None}

        nested_endpoint = f"workflow_job_templates/{workflow_target_id}/workflow_nodes/"

        try:
            result = await self.client.post(nested_endpoint, json_data=resolved)
            node_id = result["id"]

            # Step 2: create and link the approval template
            await self.client.post(
                f"workflow_job_template_nodes/{node_id}/create_approval_template/",
                json_data={
                    "name": approval_name,
                    "description": approval_description,
                    "timeout": approval_timeout,
                },
            )

            self.state.mark_completed(
                resource_type=resource_type,
                source_id=source_id,
                target_id=node_id,
                target_name=result.get("identifier", "unknown"),
            )
            self.stats["imported_count"] += 1

            logger.info(
                "workflow_approval_node_imported",
                source_id=source_id,
                target_node_id=node_id,
                approval_name=approval_name,
            )

            result["_edge_data"] = {
                "success_nodes": data.get("success_nodes", []),
                "failure_nodes": data.get("failure_nodes", []),
                "always_nodes": data.get("always_nodes", []),
            }
            result["_source_id"] = source_id
            return result

        except Exception as e:
            error_msg = f"Failed to import approval node: {e}"
            logger.error(
                "workflow_approval_node_import_failed",
                source_id=source_id,
                error=str(e),
            )
            self.stats["error_count"] += 1
            self.state.mark_failed(
                resource_type=resource_type,
                source_id=source_id,
                error_message=error_msg,
            )
            self.import_errors.append(
                {
                    "resource_type": resource_type,
                    "source_id": source_id,
                    "name": data.get("identifier", "unknown"),
                    "error": error_msg,
                    "error_type": type(e).__name__,
                }
            )
            return None

    async def _handle_system_job_node(
        self,
        source_id: int,
        data: dict[str, Any],
        workflow_target_id: int,
    ) -> dict[str, Any] | None:
        """Create a workflow node that references a system_job_template.

        System job templates (Cleanup Activity Stream, etc.) are pre-existing
        on every AAP instance with consistent names but potentially different
        IDs.  Resolved by name lookup on the target rather than id_mappings.
        """
        resource_type = "workflow_nodes"
        ujt_summary = (data.get("summary_fields") or {}).get("unified_job_template") or {}
        ujt_name = ujt_summary.get("name")
        ujt_source_id = data.get("unified_job_template")

        # Look up the system_job_template on target by name
        target_ujt_id = None
        if ujt_name:
            try:
                results = await self.client.get(
                    "system_job_templates/",
                    params={"name": ujt_name},
                )
                resources = (results or {}).get("results", [])
                if resources:
                    target_ujt_id = resources[0]["id"]
            except Exception as e:
                logger.warning(
                    "system_job_template_lookup_failed",
                    ujt_name=ujt_name,
                    error=str(e),
                )

        if not target_ujt_id:
            error_msg = (
                f"Cannot import workflow node: system_job_template '{ujt_name}' "
                f"(source_id={ujt_source_id}) not found on target"
            )
            logger.error(
                "workflow_node_system_job_not_found",
                source_id=source_id,
                ujt_name=ujt_name,
            )
            self.stats["error_count"] += 1
            self.state.mark_failed(
                resource_type=resource_type,
                source_id=source_id,
                error_message=error_msg,
            )
            self.import_errors.append(
                {
                    "resource_type": resource_type,
                    "source_id": source_id,
                    "name": data.get("identifier", "unknown"),
                    "error": error_msg,
                    "error_type": "DependencyError",
                }
            )
            return None

        # Build node payload with resolved target UJT ID
        skip_fields = {
            "id",
            "type",
            "url",
            "related",
            "summary_fields",
            "created",
            "modified",
            "natural_key",
            "success_nodes",
            "failure_nodes",
            "always_nodes",
            "_source_id",
            "_source_workflow_id",
            "_ujt_resource_type",
        }
        resolved = {k: v for k, v in data.items() if k not in skip_fields and v is not None}
        resolved["unified_job_template"] = target_ujt_id

        nested_endpoint = f"workflow_job_templates/{workflow_target_id}/workflow_nodes/"

        try:
            result = await self.client.post(nested_endpoint, json_data=resolved)

            self.state.mark_completed(
                resource_type=resource_type,
                source_id=source_id,
                target_id=result["id"],
                target_name=result.get("identifier", "unknown"),
            )
            self.stats["imported_count"] += 1

            logger.info(
                "workflow_system_job_node_imported",
                source_id=source_id,
                target_id=result["id"],
                ujt_name=ujt_name,
            )

            result["_edge_data"] = {
                "success_nodes": data.get("success_nodes", []),
                "failure_nodes": data.get("failure_nodes", []),
                "always_nodes": data.get("always_nodes", []),
            }
            result["_source_id"] = source_id
            return result

        except Exception as e:
            error_msg = f"Failed to import system job node: {e}"
            logger.error(
                "workflow_system_job_node_import_failed",
                source_id=source_id,
                error=str(e),
            )
            self.stats["error_count"] += 1
            self.state.mark_failed(
                resource_type=resource_type,
                source_id=source_id,
                error_message=error_msg,
            )
            self.import_errors.append(
                {
                    "resource_type": resource_type,
                    "source_id": source_id,
                    "name": data.get("identifier", "unknown"),
                    "error": error_msg,
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
            # Non-mutating read: keep caller dicts intact for retry passes.
            source_id = node.get("_source_id", node.get("id"))
            node_payload = {k: v for k, v in node.items() if k != "_source_id"}

            # Don't remove edge fields here - import_resource() will extract and store them
            # The edge creation happens after all nodes are imported

            try:
                result = await self.import_resource(
                    resource_type="workflow_nodes",
                    source_id=source_id,
                    data=node_payload,
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
