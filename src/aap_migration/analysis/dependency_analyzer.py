"""Cross-organization dependency analyzer for migration planning."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aap_migration.analysis.quality import QualityReport, generate_quality_report
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
        self.required_by.append({"type": resource_type, "id": resource_id, "name": resource_name})


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
    resources: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict
    )  # All resources by type
    quality_report: QualityReport | None = None  # Resource quality analysis

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
    global_resources: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    total_duplicates: int = 0  # Total duplicate resources across all orgs
    average_quality_score: float = 100.0  # Average quality score across orgs

    def get_quality_summary(self) -> dict[str, Any]:
        """Get aggregated quality statistics across all organizations."""
        total_dups = 0
        total_errors = 0
        total_warnings = 0
        org_count = 0
        total_score = 0.0

        for report in self.org_reports.values():
            if report.quality_report:
                org_count += 1
                total_dups += report.quality_report.duplicate_count
                total_score += report.quality_report.quality_score

                severity_counts = report.quality_report.get_severity_counts()
                total_errors += severity_counts.get("error", 0)
                total_warnings += severity_counts.get("warning", 0)

        avg_score = total_score / org_count if org_count > 0 else 100.0

        return {
            "total_duplicates": total_dups,
            "total_errors": total_errors,
            "total_warnings": total_warnings,
            "average_quality_score": round(avg_score, 1),
            "orgs_analyzed": org_count,
        }


class CrossOrgDependencyAnalyzer:
    """Analyzes cross-organization dependencies in AAP."""

    def __init__(
        self,
        source_client: AAPSourceClient,
        max_concurrent_orgs: int = 5,
        max_concurrent_resources: int = 20,
        progress_callback: callable = None,
        db_service: Any = None,
        use_cache: bool = True,
        cache_ttl_hours: int = 24,
    ):
        """Initialize analyzer.

        Args:
            source_client: AAP source client
            max_concurrent_orgs: Max orgs to analyze in parallel (default: 5)
            max_concurrent_resources: Max resource fetches in parallel (default: 20)
            progress_callback: Optional callback(current, total, message) for progress
            db_service: Optional DatabaseService for caching
            use_cache: Whether to use database cache (default: True)
            cache_ttl_hours: Cache TTL in hours (default: 24)
        """
        self.client = source_client
        self.max_concurrent_orgs = max_concurrent_orgs
        self.max_concurrent_resources = max_concurrent_resources
        self.progress_callback = progress_callback
        self.db_service = db_service
        self.use_cache = use_cache and db_service is not None
        self.cache_ttl_hours = cache_ttl_hours
        self._org_cache: dict[int, str] = {}
        self._resource_cache: dict[str, dict[int, dict]] = {}
        self._org_semaphore = asyncio.Semaphore(max_concurrent_orgs)
        self._resource_semaphore = asyncio.Semaphore(max_concurrent_resources)
        self._job_id: int | None = None

    async def analyze_organization(
        self, org_name: str, force_refresh: bool = False
    ) -> OrgDependencyReport:
        """Analyze dependencies for a single organization.

        Args:
            org_name: Organization name
            force_refresh: Force re-fetch from AAP even if cached

        Returns:
            OrgDependencyReport with dependency details
        """
        logger.info(
            "dependency_analysis_start",
            org_name=org_name,
            use_cache=self.use_cache,
            force_refresh=force_refresh,
            message=f"Analyzing organization: {org_name}",
        )

        # Check cache first
        if self.use_cache and not force_refresh:
            if not self.db_service.needs_analysis(org_name, self.cache_ttl_hours):
                logger.info(
                    "cache_hit", org_name=org_name, message=f"Using cached data for {org_name}"
                )
                cached = self.db_service.get_cached_analysis(org_name)
                if cached:
                    # Use cached resources
                    resources = cached["resources"]
                    org_id = cached["org_id"]

                    # Analyze dependencies from cached data
                    cross_org_deps = await self._analyze_resources(org_name, resources)

                    return OrgDependencyReport(
                        org_name=org_name,
                        org_id=org_id,
                        resource_count=cached["resource_count"],
                        has_cross_org_deps=len(cross_org_deps) > 0,
                        dependencies=cross_org_deps,
                        can_migrate_standalone=len(cross_org_deps) == 0,
                        required_migrations_before=sorted(cross_org_deps.keys()),
                        resources=resources,
                    )

        # Cache miss or force refresh - fetch from AAP
        logger.info(
            "cache_miss", org_name=org_name, message=f"Fetching fresh data from AAP for {org_name}"
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
            message=f"Fetched {total_resources} resources from {len(resources)} types",
        )

        # Analyze dependencies
        cross_org_deps = await self._analyze_resources(org_name, resources)

        # Generate quality report (duplicate detection)
        quality_report = generate_quality_report(resources, org_name)
        logger.info(
            "quality_analysis_complete",
            org_name=org_name,
            duplicate_count=quality_report.duplicate_count,
            quality_score=quality_report.quality_score,
            message=(
                f"Quality: {quality_report.duplicate_count} duplicates, "
                f"score: {quality_report.quality_score}"
            ),
        )

        # Build report
        report = OrgDependencyReport(
            org_name=org_name,
            org_id=org_id,
            resource_count=total_resources,
            has_cross_org_deps=len(cross_org_deps) > 0,
            dependencies=cross_org_deps,
            can_migrate_standalone=len(cross_org_deps) == 0,
            required_migrations_before=sorted(cross_org_deps.keys()),
            resources=resources,
            quality_report=quality_report,
        )

        # Save to database cache
        if self.use_cache:
            try:
                db_org = self.db_service.upsert_organization(
                    aap_id=org_id,
                    name=org_name,
                    resource_count=total_resources,
                    has_dependencies=len(cross_org_deps) > 0,
                    can_migrate_standalone=len(cross_org_deps) == 0,
                    last_modified_at=org.get("modified"),
                )

                # Save resources to cache
                for resource_type, items in resources.items():
                    if items:
                        self.db_service.bulk_upsert_resources(
                            org_id=db_org.id,
                            resource_type=resource_type,
                            resources=items,
                        )

                logger.info(
                    "cache_updated",
                    org_name=org_name,
                    total_resources=total_resources,
                    message=f"Cached {total_resources} resources for {org_name}",
                )
            except Exception as e:
                logger.warning(
                    "cache_update_failed",
                    org_name=org_name,
                    error=str(e),
                    message=f"Failed to cache data for {org_name}: {e}",
                )

        logger.info(
            "dependency_analysis_complete",
            org_name=org_name,
            has_cross_org_deps=report.has_cross_org_deps,
            dependency_orgs=len(cross_org_deps),
            message=f"Analysis complete for {org_name}",
        )

        return report

    async def _analyze_org_with_progress(
        self, org_name: str, current: int, total: int
    ) -> tuple[str, OrgDependencyReport]:
        """Analyze single org with semaphore and progress tracking.

        Args:
            org_name: Organization name
            current: Current progress count
            total: Total organizations

        Returns:
            Tuple of (org_name, report)
        """
        async with self._org_semaphore:
            if self.progress_callback:
                self.progress_callback(current, total, f"Analyzing {org_name}")

            logger.info(
                "dependency_analysis_progress",
                org_name=org_name,
                progress=f"{current}/{total}",
                message=f"Analyzing {org_name} ({current}/{total})",
            )

            try:
                report = await self.analyze_organization(org_name)
                return (org_name, report)
            except Exception as e:
                logger.error(
                    "dependency_analysis_org_failed",
                    org_name=org_name,
                    error=str(e),
                    message=f"Failed to analyze {org_name}: {e}",
                )
                # Return minimal report on error
                return (
                    org_name,
                    OrgDependencyReport(
                        org_name=org_name,
                        org_id=-1,
                        resource_count=0,
                        has_cross_org_deps=False,
                        dependencies={},
                        can_migrate_standalone=True,
                        required_migrations_before=[],
                        resources={},
                    ),
                )

    async def analyze_all_organizations(
        self, force_refresh: bool = False
    ) -> GlobalDependencyReport:
        """Analyze all organizations in source AAP.

        Args:
            force_refresh: Force re-fetch from AAP even if cached

        Returns:
            GlobalDependencyReport with complete analysis
        """
        # Get all organizations
        orgs = await self.client.get_paginated("organizations/")
        org_names = sorted([org["name"] for org in orgs])

        # Create analysis job if database enabled
        if self.use_cache:
            self._job_id = self.db_service.create_analysis_job(
                job_type="full" if force_refresh else "auto", total_orgs=len(org_names)
            )
            logger.info("analysis_job_created", job_id=self._job_id)

        logger.info(
            "dependency_analysis_all_start",
            total_orgs=len(org_names),
            max_concurrent=self.max_concurrent_orgs,
            use_cache=self.use_cache,
            force_refresh=force_refresh,
            message=f"Analyzing {len(org_names)} orgs (max {self.max_concurrent_orgs} parallel)",
        )

        if self.progress_callback:
            self.progress_callback(0, len(org_names), "Starting analysis...")

        # Analyze orgs in parallel with progress tracking
        org_reports = {}

        # Create tasks for parallel execution
        tasks = [
            self._analyze_org_with_progress(org_name, i + 1, len(org_names))
            for i, org_name in enumerate(org_names)
        ]

        # Execute all tasks in parallel (semaphore limits concurrency)
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect results
        for result in results:
            if isinstance(result, Exception):
                logger.error(
                    "dependency_analysis_task_exception",
                    error=str(result),
                    message=f"Task failed with exception: {result}",
                )
                continue

            org_name, report = result
            org_reports[org_name] = report

        # Fetch global resources (not tied to any organization)
        logger.info("fetching_global_resources", message="Fetching global resources")
        global_resources = await self._fetch_global_resources()
        logger.info(
            "global_resources_fetched",
            total_resources=sum(len(items) for items in global_resources.values()),
            resource_types=len(global_resources),
        )

        # Separate independent vs dependent
        independent = sorted([name for name, r in org_reports.items() if not r.has_cross_org_deps])
        dependent = sorted([name for name, r in org_reports.items() if r.has_cross_org_deps])

        # Calculate migration order
        from aap_migration.analysis.graph import (
            group_into_phases,
            topological_sort,
        )

        graph = {org: report.required_migrations_before for org, report in org_reports.items()}
        migration_order = topological_sort(graph)
        migration_phases = group_into_phases(graph, migration_order)

        # Calculate quality summary
        total_duplicates = sum(
            report.quality_report.duplicate_count
            for report in org_reports.values()
            if report.quality_report
        )
        quality_scores = [
            report.quality_report.quality_score
            for report in org_reports.values()
            if report.quality_report
        ]
        average_quality_score = (
            sum(quality_scores) / len(quality_scores) if quality_scores else 100.0
        )

        logger.info(
            "dependency_analysis_all_complete",
            total_orgs=len(org_names),
            independent_orgs=len(independent),
            dependent_orgs=len(dependent),
            migration_phases=len(migration_phases),
            total_duplicates=total_duplicates,
            average_quality_score=round(average_quality_score, 1),
            message="Global analysis complete",
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
            global_resources=global_resources,
            total_duplicates=total_duplicates,
            average_quality_score=round(average_quality_score, 1),
        )

    async def _get_organization(self, org_name: str) -> dict:
        """Fetch organization by name."""
        orgs = await self.client.get_paginated("organizations/", params={"name": org_name})
        if not orgs:
            raise ValueError(f"Organization not found: {org_name}")
        return orgs[0]

    async def _fetch_org_resources(self, org_id: int, org_name: str) -> dict[str, list[dict]]:
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

        # Fetch all resource types in parallel
        async def fetch_resource_type(rtype: str) -> tuple[str, list[dict]]:
            """Fetch single resource type with semaphore."""
            async with self._resource_semaphore:
                try:
                    endpoint = f"{rtype}/"
                    items = await self.client.get_paginated(
                        endpoint, params={"organization": org_id}
                    )
                    logger.debug(
                        "dependency_analysis_resource_fetch",
                        org_name=org_name,
                        resource_type=rtype,
                        count=len(items),
                    )
                    return (rtype, items)
                except Exception as e:
                    logger.warning(
                        "dependency_analysis_resource_fetch_failed",
                        org_name=org_name,
                        resource_type=rtype,
                        error=str(e),
                    )
                    return (rtype, [])

        # Execute all resource fetches in parallel
        tasks = [fetch_resource_type(rtype) for rtype in resource_types]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect results
        resources = {}
        for result in results:
            if isinstance(result, Exception):
                logger.error(
                    "dependency_analysis_resource_exception", org_name=org_name, error=str(result)
                )
                continue
            rtype, items = result
            resources[rtype] = items

        return resources

    async def _fetch_global_resources(self) -> dict[str, list[dict]]:
        """Fetch global resources (not tied to any organization).

        Returns:
            Dictionary mapping resource type to list of resources
        """
        # Resource types that can be global (organization=null)
        global_resource_types = [
            "credential_types",
            "execution_environments",
            "instance_groups",
            "notification_templates",
        ]

        resources = {}
        for rtype in global_resource_types:
            try:
                endpoint = f"{rtype}/"
                # Fetch resources with no organization
                # Note: Some endpoints may not support organization__isnull filter
                try:
                    items = await self.client.get_paginated(
                        endpoint, params={"organization__isnull": "true"}
                    )
                except Exception:
                    # Fallback: fetch all and filter client-side
                    all_items = await self.client.get_paginated(endpoint)
                    items = [item for item in all_items if item.get("organization") is None]

                resources[rtype] = items
                logger.debug("global_resource_fetch", resource_type=rtype, count=len(items))
            except Exception as e:
                logger.warning("global_resource_fetch_failed", resource_type=rtype, error=str(e))
                resources[rtype] = []

        return resources

    async def _analyze_resources(
        self, org_name: str, resources: dict[str, list[dict]]
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
                            (
                                d
                                for d in cross_org_deps[target_org]
                                if d.resource_id == target_id
                                and d.resource_type == (target_type or "unknown")
                            ),
                            None,
                        )

                        if existing:
                            # Add to required_by
                            existing.add_usage(
                                resource_type, resource["id"], resource.get("name", "unknown")
                            )
                        else:
                            # New dependency
                            target_name = await self._get_resource_name(target_type, target_id)
                            dep = ResourceDependency(
                                resource_type=target_type or "unknown",
                                resource_id=target_id,
                                resource_name=target_name,
                                org_name=target_org,
                            )
                            dep.add_usage(
                                resource_type, resource["id"], resource.get("name", "unknown")
                            )
                            cross_org_deps[target_org].append(dep)

        return cross_org_deps

    async def _get_resource_org(self, resource_type: str | None, resource_id: int) -> str | None:
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
                error=str(e),
            )

        return None

    async def _get_resource_name(self, resource_type: str | None, resource_id: int) -> str:
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
        except Exception:
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
        except Exception:
            return f"org_{org_id}"
