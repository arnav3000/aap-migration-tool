"""
Organization mapper for migration reports.

This module provides functionality to map failed/skipped resources to their
organizations, enabling organization-scoped failure analysis.
"""

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


# Resource types that are organization-scoped
ORG_SCOPED_RESOURCES = {
    "credentials",
    "projects",
    "inventories",
    "job_templates",
    "workflow_job_templates",
    "notification_templates",
    "applications",
    "teams",
}

# Resource types that are globally scoped (no organization)
GLOBAL_RESOURCES = {
    "organizations",
    "credential_types",
    "execution_environments",
    "instance_groups",
    "settings",
    "labels",
}

# Parent-scoped resources (inherit org from parent)
PARENT_SCOPED_RESOURCES = {
    "inventory_sources": "inventory",
    "inventory_groups": "inventory",
    "hosts": "inventory",
    "workflow_nodes": "workflow",
}


class OrganizationMapper:
    """Maps migration resources to their organizations."""

    def __init__(self, export_dir: Path, transform_dir: Path):
        """Initialize organization mapper.

        Args:
            export_dir: Directory containing exported resources
            transform_dir: Directory containing transformed resources
        """
        self.export_dir = Path(export_dir)
        self.transform_dir = Path(transform_dir)

        # Caches
        self.org_names: dict[int, str] = {}  # org_id -> org_name
        self.resource_orgs: dict[str, dict[int, int | None]] = {}  # resource_type -> {source_id -> org_id}

        # Load organization names
        self._load_organizations()

    def _load_organizations(self) -> None:
        """Load organization ID to name mapping."""
        org_dir = self.export_dir / "organizations"
        if not org_dir.exists():
            logger.warning("organization_export_not_found", message="No organizations exported")
            return

        for json_file in org_dir.glob("*.json"):
            try:
                with open(json_file) as f:
                    orgs = json.load(f)
                    for org in orgs:
                        org_id = org.get("id")
                        org_name = org.get("name")
                        if org_id and org_name:
                            self.org_names[org_id] = org_name
            except Exception as e:
                logger.warning("failed_to_load_orgs", file=str(json_file), error=str(e))

        logger.info("loaded_organizations", count=len(self.org_names))

    def _load_resource_orgs(self, resource_type: str) -> None:
        """Load organization mappings for a resource type.

        Args:
            resource_type: Type of resource to load
        """
        if resource_type in self.resource_orgs:
            return  # Already loaded

        self.resource_orgs[resource_type] = {}

        # Try export directory first
        resource_dir = self.export_dir / resource_type
        if not resource_dir.exists():
            logger.debug("resource_export_not_found", resource_type=resource_type)
            return

        for json_file in resource_dir.glob("*.json"):
            try:
                with open(json_file) as f:
                    resources = json.load(f)
                    for resource in resources:
                        source_id = resource.get("id") or resource.get("_source_id")
                        if not source_id:
                            continue

                        # Get organization based on resource type
                        org_id = self._extract_org_from_resource(resource_type, resource)
                        self.resource_orgs[resource_type][source_id] = org_id

            except Exception as e:
                logger.warning(
                    "failed_to_load_resource_orgs",
                    resource_type=resource_type,
                    file=str(json_file),
                    error=str(e),
                )

        logger.debug(
            "loaded_resource_orgs",
            resource_type=resource_type,
            count=len(self.resource_orgs[resource_type]),
        )

    def _extract_org_from_resource(self, resource_type: str, resource: dict[str, Any]) -> int | None:
        """Extract organization ID from a resource.

        Args:
            resource_type: Type of resource
            resource: Resource data

        Returns:
            Organization ID or None
        """
        # Direct organization field
        if resource_type in ORG_SCOPED_RESOURCES:
            org_id = resource.get("organization")
            if org_id is not None:
                return org_id

        # Parent-scoped resources use summary_fields
        if resource_type in PARENT_SCOPED_RESOURCES:
            summary = resource.get("summary_fields", {})
            org = summary.get("organization", {})
            org_id = org.get("id") if isinstance(org, dict) else None
            if org_id is not None:
                return org_id

        # Schedules: trace through unified_job_template
        if resource_type == "schedules":
            ujt_id = resource.get("unified_job_template")
            if ujt_id:
                # Try job_templates first
                if "job_templates" not in self.resource_orgs:
                    self._load_resource_orgs("job_templates")
                org_id = self.resource_orgs.get("job_templates", {}).get(ujt_id)
                if org_id is not None:
                    return org_id

                # Try workflow_job_templates
                if "workflow_job_templates" not in self.resource_orgs:
                    self._load_resource_orgs("workflow_job_templates")
                org_id = self.resource_orgs.get("workflow_job_templates", {}).get(ujt_id)
                if org_id is not None:
                    return org_id

        return None

    def get_organization_name(self, resource_type: str, source_id: int) -> str:
        """Get organization name for a resource.

        Args:
            resource_type: Type of resource
            source_id: Source ID of resource

        Returns:
            Organization name, "(Global)", or "(Unknown)"
        """
        # Global resources
        if resource_type in GLOBAL_RESOURCES:
            return "(Global)"

        # Load resource org mappings if not cached
        if resource_type not in self.resource_orgs:
            self._load_resource_orgs(resource_type)

        # Get org ID for this resource
        org_id = self.resource_orgs.get(resource_type, {}).get(source_id)

        if org_id is None:
            return "(Unknown)"

        # Resolve org name
        return self.org_names.get(org_id, f"(Org ID {org_id})")

    def build_org_summary(self, failures: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Build organization summary from failure list.

        Args:
            failures: List of failed resources with keys:
                - resource_type
                - source_id
                - source_name
                - status
                - error_message

        Returns:
            Dictionary mapping org_name to summary:
                - failed: count
                - skipped: count
                - total: count
                - resource_types: set of affected resource types
                - resources: list of resource details
        """
        org_summary = defaultdict(lambda: {
            "failed": 0,
            "skipped": 0,
            "total": 0,
            "resource_types": set(),
            "resources": [],
        })

        for failure in failures:
            resource_type = failure.get("resource_type")
            source_id = failure.get("source_id")
            status = failure.get("status", "failed")

            if not resource_type or source_id is None:
                continue

            # Get organization name
            org_name = self.get_organization_name(resource_type, source_id)

            # Update summary
            summary = org_summary[org_name]
            if status == "skipped":
                summary["skipped"] += 1
            else:
                summary["failed"] += 1
            summary["total"] += 1
            summary["resource_types"].add(resource_type)
            summary["resources"].append(failure)

        return dict(org_summary)
