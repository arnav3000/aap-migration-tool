"""Cross-organization dependency analyzer for migration planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aap_migration.client.aap_source_client import AAPSourceClient
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


# Resource type foreign key mappings
# Maps resource_type -> list of (field_name, target_resource_type)
RESOURCE_DEPENDENCIES = {
    "job_templates": [
        ("project", "projects"),
        ("inventory", "inventories"),
        ("credential", "credentials"),
        ("execution_environment", "execution_environments"),
    ],
    "inventory_sources": [
        ("inventory", "inventories"),
        ("source_project", "projects"),
        ("credential", "credentials"),
    ],
    "workflow_job_templates": [
        ("organization", "organizations"),
        ("inventory", "inventories"),
    ],
    "schedules": [
        ("unified_job_template", None),  # Can be job_template or workflow
    ],
    "hosts": [
        ("inventory", "inventories"),
    ],
    "inventory_groups": [
        ("inventory", "inventories"),
    ],
    "credential_input_sources": [
        ("source_credential", "credentials"),
        ("target_credential", "credentials"),
    ],
}


@dataclass
class ResourceDependency:
    """A single resource dependency from another organization."""

    resource_type: str
    resource_id: int
    resource_name: str
    org_name: str
    required_by: list[dict[str, Any]] = field(default_factory=list)

    def add_usage(self, resource_type: str, resource_id: int, resource_name: str):
        """Add a resource that requires this dependency."""
        self.required_by.append({
            "type": resource_type,
            "id": resource_id,
            "name": resource_name
        })


@dataclass
class OrgDependencyReport:
    """Dependency analysis report for a single organization."""

    org_name: str
    org_id: int
    resource_count: int
    has_cross_org_deps: bool
    dependencies: dict[str, list[ResourceDependency]]  # org_name -> resources
    can_migrate_standalone: bool
    required_migrations_before: list[str]

    def get_total_cross_org_resources(self) -> int:
        """Count total cross-org resource dependencies."""
        return sum(len(deps) for deps in self.dependencies.values())


@dataclass
class GlobalDependencyReport:
    """Global dependency analysis across all organizations."""

    analysis_date: datetime
    source_url: str
    total_organizations: int
    analyzed_organizations: list[str]
    independent_orgs: list[str]
    dependent_orgs: list[str]
    org_reports: dict[str, OrgDependencyReport]
    migration_order: list[str]
    migration_phases: list[dict[str, Any]]


class CrossOrgDependencyAnalyzer:
    """Analyzes cross-organization dependencies in AAP."""

    def __init__(self, source_client: AAPSourceClient):
        """Initialize analyzer.

        Args:
            source_client: AAP source client
        """
        self.client = source_client
        self._org_cache: dict[int, str] = {}
        self._resource_cache: dict[str, dict[int, dict]] = {}

    async def analyze_organization(self, org_name: str) -> OrgDependencyReport:
        """Analyze dependencies for a single organization.

        Args:
            org_name: Organization name

        Returns:
            OrgDependencyReport with dependency details
        """
        logger.info(
            "dependency_analysis_start",
            org_name=org_name,
            message=f"Analyzing organization: {org_name}"
        )

        # Get org ID
        org = await self._get_organization(org_name)
        org_id = org["id"]

        # Fetch all resources for this org
        resources = await self._fetch_org_resources(org_id, org_name)

        total_resources = sum(len(r) for r in resources.values())
        logger.info(
            "dependency_analysis_resources_fetched",
            org_name=org_name,
            total_resources=total_resources,
            resource_types=len(resources),
            message=f"Fetched {total_resources} resources from {len(resources)} types"
        )

        # Analyze dependencies
        cross_org_deps = await self._analyze_resources(org_name, resources)

        # Build report
        report = OrgDependencyReport(
            org_name=org_name,
            org_id=org_id,
            resource_count=total_resources,
            has_cross_org_deps=len(cross_org_deps) > 0,
            dependencies=cross_org_deps,
            can_migrate_standalone=len(cross_org_deps) == 0,
            required_migrations_before=sorted(cross_org_deps.keys()),
        )

        logger.info(
            "dependency_analysis_complete",
            org_name=org_name,
            has_cross_org_deps=report.has_cross_org_deps,
            dependency_orgs=len(cross_org_deps),
            message=f"Analysis complete for {org_name}"
        )

        return report

    async def analyze_all_organizations(self) -> GlobalDependencyReport:
        """Analyze all organizations in source AAP.

        Returns:
            GlobalDependencyReport with complete analysis
        """
        # Get all organizations
        orgs = await self.client.get_paginated("organizations/")
        org_names = sorted([org["name"] for org in orgs])

        logger.info(
            "dependency_analysis_all_start",
            total_orgs=len(org_names),
            message=f"Analyzing {len(org_names)} organizations"
        )

        # Analyze each org
        org_reports = {}
        for i, org_name in enumerate(org_names, 1):
            logger.info(
                "dependency_analysis_progress",
                org_name=org_name,
                progress=f"{i}/{len(org_names)}",
                message=f"Analyzing {org_name} ({i}/{len(org_names)})"
            )
            report = await self.analyze_organization(org_name)
            org_reports[org_name] = report

        # Separate independent vs dependent
        independent = sorted([name for name, r in org_reports.items()
                              if not r.has_cross_org_deps])
        dependent = sorted([name for name, r in org_reports.items()
                            if r.has_cross_org_deps])

        # Calculate migration order
        from aap_migration.analysis.dependency_graph import (
            group_into_phases,
            topological_sort,
        )

        graph = {org: report.required_migrations_before
                 for org, report in org_reports.items()}
        migration_order = topological_sort(graph)
        migration_phases = group_into_phases(graph, migration_order)

        logger.info(
            "dependency_analysis_all_complete",
            total_orgs=len(org_names),
            independent_orgs=len(independent),
            dependent_orgs=len(dependent),
            migration_phases=len(migration_phases),
            message="Global analysis complete"
        )

        return GlobalDependencyReport(
            analysis_date=datetime.now(),
            source_url=str(self.client.base_url),
            total_organizations=len(org_names),
            analyzed_organizations=org_names,
            independent_orgs=independent,
            dependent_orgs=dependent,
            org_reports=org_reports,
            migration_order=migration_order,
            migration_phases=migration_phases,
        )

    async def _get_organization(self, org_name: str) -> dict:
        """Fetch organization by name."""
        orgs = await self.client.get_paginated("organizations/", params={"name": org_name})
        if not orgs:
            raise ValueError(f"Organization not found: {org_name}")
        return orgs[0]

    async def _fetch_org_resources(
        self,
        org_id: int,
        org_name: str
    ) -> dict[str, list[dict]]:
        """Fetch all resources for an organization."""
        # Resource types to analyze (that can have cross-org dependencies)
        resource_types = [
            "teams",
            "credentials",
            "projects",
            "inventories",
            "inventory_sources",
            "hosts",
            "inventory_groups",
            "job_templates",
            "workflow_job_templates",
            "schedules",
            "notification_templates",
            "credential_input_sources",
        ]

        resources = {}
        for rtype in resource_types:
            try:
                # Build endpoint with trailing slash
                endpoint = f"{rtype}/"
                items = await self.client.get_paginated(endpoint, params={"organization": org_id})
                resources[rtype] = items
                logger.debug(
                    "dependency_analysis_resource_fetch",
                    org_name=org_name,
                    resource_type=rtype,
                    count=len(items)
                )
            except Exception as e:
                logger.warning(
                    "dependency_analysis_resource_fetch_failed",
                    org_name=org_name,
                    resource_type=rtype,
                    error=str(e)
                )
                resources[rtype] = []

        return resources

    async def _analyze_resources(
        self,
        org_name: str,
        resources: dict[str, list[dict]]
    ) -> dict[str, list[ResourceDependency]]:
        """Analyze resources for cross-org dependencies."""
        cross_org_deps: dict[str, list[ResourceDependency]] = {}

        for resource_type, items in resources.items():
            # Get FK fields for this resource type
            fk_fields = RESOURCE_DEPENDENCIES.get(resource_type, [])
            if not fk_fields:
                continue

            for resource in items:
                # Check each FK field
                for field_name, target_type in fk_fields:
                    target_id = resource.get(field_name)
                    if not target_id:
                        continue

                    # Get target resource org
                    target_org = await self._get_resource_org(target_type, target_id)

                    if target_org and target_org != org_name:
                        # Cross-org dependency found!
                        if target_org not in cross_org_deps:
                            cross_org_deps[target_org] = []

                        # Check if already tracked
                        existing = next(
                            (d for d in cross_org_deps[target_org]
                             if d.resource_id == target_id and d.resource_type == (target_type or "unknown")),
                            None
                        )

                        if existing:
                            # Add to required_by
                            existing.add_usage(
                                resource_type,
                                resource["id"],
                                resource.get("name", "unknown")
                            )
                        else:
                            # New dependency
                            target_name = await self._get_resource_name(
                                target_type, target_id
                            )
                            dep = ResourceDependency(
                                resource_type=target_type or "unknown",
                                resource_id=target_id,
                                resource_name=target_name,
                                org_name=target_org,
                            )
                            dep.add_usage(
                                resource_type,
                                resource["id"],
                                resource.get("name", "unknown")
                            )
                            cross_org_deps[target_org].append(dep)

        return cross_org_deps

    async def _get_resource_org(
        self,
        resource_type: str | None,
        resource_id: int
    ) -> str | None:
        """Get organization name for a resource."""
        if not resource_type:
            return None

        # Check cache
        if resource_type in self._resource_cache:
            if resource_id in self._resource_cache[resource_type]:
                resource = self._resource_cache[resource_type][resource_id]
                org_id = resource.get("organization")
                if org_id:
                    return await self._get_org_name(org_id)

        # Fetch resource
        try:
            endpoint = f"{resource_type}/{resource_id}/"
            resource = await self.client.get(endpoint)

            # Cache it
            if resource_type not in self._resource_cache:
                self._resource_cache[resource_type] = {}
            self._resource_cache[resource_type][resource_id] = resource

            # Get org
            org_id = resource.get("organization")
            if org_id:
                return await self._get_org_name(org_id)
        except Exception as e:
            logger.debug(
                "dependency_analysis_resource_fetch_error",
                resource_type=resource_type,
                resource_id=resource_id,
                error=str(e)
            )

        return None

    async def _get_resource_name(
        self,
        resource_type: str | None,
        resource_id: int
    ) -> str:
        """Get resource name."""
        if not resource_type:
            return f"resource_{resource_id}"

        if resource_type in self._resource_cache:
            if resource_id in self._resource_cache[resource_type]:
                return self._resource_cache[resource_type][resource_id].get(
                    "name", f"{resource_type}_{resource_id}"
                )

        try:
            endpoint = f"{resource_type}/{resource_id}/"
            resource = await self.client.get(endpoint)
            return resource.get("name", f"{resource_type}_{resource_id}")
        except:
            return f"{resource_type}_{resource_id}"

    async def _get_org_name(self, org_id: int) -> str:
        """Get organization name from ID."""
        if org_id in self._org_cache:
            return self._org_cache[org_id]

        try:
            endpoint = f"organizations/{org_id}/"
            org = await self.client.get(endpoint)
            self._org_cache[org_id] = org["name"]
            return org["name"]
        except:
            return f"org_{org_id}"
