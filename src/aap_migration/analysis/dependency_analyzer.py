"""Cross-organization dependency analyzer for migration planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aap_migration.client.aap_source_client import AAPSourceClient
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


# Resource type foreign-key mappings.
#
# Each entry: (path, target_type, kind)
#   - path: dotted path within the resource dict where the FK lives
#     (e.g. "credential", "summary_fields.credentials").
#   - target_type: the AAP resource type the FK points at, used to look up
#     org membership. None for polymorphic refs (e.g. schedules'
#     unified_job_template can be a JT or a WJT).
#   - kind: "single" (path resolves to a scalar int ID) or "many" (path
#     resolves to a list of dicts each carrying an "id").
#
# An org can depend on *multiple* other orgs through any combination of
# these — both because a single resource may have several distinct
# single-FKs pointing into different orgs, and because many-FKs (e.g.
# the M2M credential set on a job template) can themselves span
# multiple owning orgs in a single field.
RESOURCE_DEPENDENCIES: dict[str, list[tuple[str, str | None, str]]] = {
    "job_templates": [
        ("project", "projects", "single"),
        ("inventory", "inventories", "single"),
        ("execution_environment", "execution_environments", "single"),
        # The legacy singular `credential` field is deprecated on modern
        # AAP — the full credential set lives at summary_fields.credentials
        # and legitimately spans multiple organizations (e.g. machine cred
        # from Org A + vault cred from Org B + cloud cred from Org C).
        # Reading only the singular field is why one JT was previously
        # never able to surface deps on more than one other org through
        # its credentials.
        ("summary_fields.credentials", "credentials", "many"),
    ],
    "projects": [
        # SCM credential, default EE, signature-validation credential all
        # commonly cross orgs in shared-content setups.
        ("credential", "credentials", "single"),
        ("default_environment", "execution_environments", "single"),
        ("signature_validation_credential", "credentials", "single"),
    ],
    "inventory_sources": [
        ("inventory", "inventories", "single"),
        ("source_project", "projects", "single"),
        ("credential", "credentials", "single"),
        ("execution_environment", "execution_environments", "single"),
    ],
    "workflow_job_templates": [
        ("inventory", "inventories", "single"),
        # Workflow nodes (a separate endpoint) can also carry cross-org
        # refs; not fetched here. Add via a follow-up if needed.
    ],
    "schedules": [
        ("unified_job_template", None, "single"),
    ],
    "hosts": [
        ("inventory", "inventories", "single"),
    ],
    "inventory_groups": [
        ("inventory", "inventories", "single"),
    ],
    "credential_input_sources": [
        ("source_credential", "credentials", "single"),
        ("target_credential", "credentials", "single"),
    ],
}


def _extract_target_ids(
    resource: dict[str, Any],
    path: str,
    kind: str,
) -> list[int]:
    """Resolve a dotted path inside a resource to a list of FK target IDs.

    Returns an empty list when the path is absent, null, or the value at
    the end is empty. For ``kind="single"`` the result has at most one
    element; for ``kind="many"`` it can have any number — this is what
    allows a single resource to surface multiple cross-org dependencies.
    """
    current: Any = resource
    for part in path.split("."):
        if not isinstance(current, dict):
            return []
        current = current.get(part)
        if current is None:
            return []

    if kind == "single":
        if isinstance(current, int):
            return [current]
        # Some endpoints embed the full related object instead of just the ID
        if isinstance(current, dict) and isinstance(current.get("id"), int):
            return [current["id"]]
        return []

    if kind == "many":
        if not isinstance(current, list):
            return []
        ids: list[int] = []
        for item in current:
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                ids.append(item["id"])
            elif isinstance(item, int):
                ids.append(item)
        return ids

    return []


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
            "name": resource_name,
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
    resources: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

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
    cycles: list[list[str]] = field(default_factory=list)


class CrossOrgDependencyAnalyzer:
    """Analyzes cross-organization dependencies in AAP."""

    def __init__(self, source_client: AAPSourceClient):
        """Initialize analyzer."""
        self.client = source_client
        self._org_cache: dict[int, str] = {}
        self._resource_cache: dict[str, dict[int, dict]] = {}

    async def analyze_organization(self, org_name: str) -> OrgDependencyReport:
        """Analyze dependencies for a single organization."""
        logger.info(
            "dependency_analysis_start",
            org_name=org_name,
            message=f"Analyzing organization: {org_name}",
        )

        org = await self._get_organization(org_name)
        org_id = org["id"]

        resources = await self._fetch_org_resources(org_id, org_name)

        total_resources = sum(len(r) for r in resources.values())
        logger.info(
            "dependency_analysis_resources_fetched",
            org_name=org_name,
            total_resources=total_resources,
            resource_types=len(resources),
            message=f"Fetched {total_resources} resources from {len(resources)} types",
        )

        cross_org_deps = await self._analyze_resources(org_name, resources)

        report = OrgDependencyReport(
            org_name=org_name,
            org_id=org_id,
            resource_count=total_resources,
            has_cross_org_deps=len(cross_org_deps) > 0,
            dependencies=cross_org_deps,
            can_migrate_standalone=len(cross_org_deps) == 0,
            required_migrations_before=sorted(cross_org_deps.keys()),
            resources=resources,
        )

        logger.info(
            "dependency_analysis_complete",
            org_name=org_name,
            has_cross_org_deps=report.has_cross_org_deps,
            dependency_orgs=len(cross_org_deps),
            message=f"Analysis complete for {org_name}",
        )

        return report

    async def analyze_all_organizations(self) -> GlobalDependencyReport:
        """Analyze all organizations in source AAP."""
        orgs = await self.client.get_paginated("organizations/")
        org_names = sorted([org["name"] for org in orgs])

        logger.info(
            "dependency_analysis_all_start",
            total_orgs=len(org_names),
            message=f"Analyzing {len(org_names)} organizations",
        )

        org_reports = {}
        for i, org_name in enumerate(org_names, 1):
            logger.info(
                "dependency_analysis_progress",
                org_name=org_name,
                progress=f"{i}/{len(org_names)}",
                message=f"Analyzing {org_name} ({i}/{len(org_names)})",
            )
            report = await self.analyze_organization(org_name)
            org_reports[org_name] = report

        independent = sorted(
            [name for name, r in org_reports.items() if not r.has_cross_org_deps]
        )
        dependent = sorted(
            [name for name, r in org_reports.items() if r.has_cross_org_deps]
        )

        from aap_migration.analysis.dependency_graph import (
            find_cycles,
            group_into_phases,
            topological_sort,
        )

        graph = {
            org: report.required_migrations_before
            for org, report in org_reports.items()
        }

        cycles = find_cycles(graph)
        migration_order = topological_sort(graph)
        migration_phases = group_into_phases(graph, migration_order)

        if cycles:
            for cycle in cycles:
                logger.warning(
                    "dependency_analysis_cycle_detected",
                    cycle=cycle,
                    cycle_size=len(cycle),
                    message=(
                        f"Cyclic dependency between {len(cycle)} orgs: "
                        f"{cycle}. These must be migrated as a unit, or the "
                        f"cross-references must be broken in the source."
                    ),
                )

        logger.info(
            "dependency_analysis_all_complete",
            total_orgs=len(org_names),
            independent_orgs=len(independent),
            dependent_orgs=len(dependent),
            migration_phases=len(migration_phases),
            cycles_detected=len(cycles),
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
            cycles=cycles,
        )

    async def _get_organization(self, org_name: str) -> dict:
        """Fetch organization by name."""
        orgs = await self.client.get_paginated(
            "organizations/", params={"name": org_name}
        )
        if not orgs:
            raise ValueError(f"Organization not found: {org_name}")
        return orgs[0]

    async def _fetch_org_resources(
        self,
        org_id: int,
        org_name: str,
    ) -> dict[str, list[dict]]:
        """Fetch all resources for an organization."""
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

        resources: dict[str, list[dict]] = {}
        for rtype in resource_types:
            try:
                endpoint = f"{rtype}/"
                items = await self.client.get_paginated(
                    endpoint, params={"organization": org_id}
                )
                resources[rtype] = items
                logger.debug(
                    "dependency_analysis_resource_fetch",
                    org_name=org_name,
                    resource_type=rtype,
                    count=len(items),
                )
            except Exception as e:
                logger.warning(
                    "dependency_analysis_resource_fetch_failed",
                    org_name=org_name,
                    resource_type=rtype,
                    error=str(e),
                )
                resources[rtype] = []

        return resources

    async def _analyze_resources(
        self,
        org_name: str,
        resources: dict[str, list[dict]],
    ) -> dict[str, list[ResourceDependency]]:
        """Analyze resources for cross-org dependencies.

        Handles both single-valued and multi-valued (M2M) FK fields. A
        single local resource may therefore contribute deps on multiple
        distinct target orgs — and the result dict can naturally hold
        any number of target-org keys.
        """
        cross_org_deps: dict[str, list[ResourceDependency]] = {}

        for resource_type, items in resources.items():
            fk_fields = RESOURCE_DEPENDENCIES.get(resource_type, [])
            if not fk_fields:
                continue

            for resource in items:
                for path, target_type, kind in fk_fields:
                    target_ids = _extract_target_ids(resource, path, kind)
                    for target_id in target_ids:
                        await self._track_cross_org_dep(
                            org_name,
                            resource,
                            resource_type,
                            target_type,
                            target_id,
                            cross_org_deps,
                        )

        return cross_org_deps

    async def _track_cross_org_dep(
        self,
        org_name: str,
        local_resource: dict,
        local_resource_type: str,
        target_type: str | None,
        target_id: int,
        cross_org_deps: dict[str, list[ResourceDependency]],
    ) -> None:
        """Record a single FK lookup as a cross-org dep when applicable."""
        target_org = await self._get_resource_org(target_type, target_id)
        if not target_org or target_org == org_name:
            return

        if target_org not in cross_org_deps:
            cross_org_deps[target_org] = []

        type_key = target_type or "unknown"
        existing = next(
            (
                d
                for d in cross_org_deps[target_org]
                if d.resource_id == target_id and d.resource_type == type_key
            ),
            None,
        )

        if existing is not None:
            existing.add_usage(
                local_resource_type,
                local_resource["id"],
                local_resource.get("name", "unknown"),
            )
            return

        target_name = await self._get_resource_name(target_type, target_id)
        dep = ResourceDependency(
            resource_type=type_key,
            resource_id=target_id,
            resource_name=target_name,
            org_name=target_org,
        )
        dep.add_usage(
            local_resource_type,
            local_resource["id"],
            local_resource.get("name", "unknown"),
        )
        cross_org_deps[target_org].append(dep)

    async def _get_resource_org(
        self,
        resource_type: str | None,
        resource_id: int,
    ) -> str | None:
        """Get organization name for a resource."""
        if not resource_type:
            return None

        if resource_type in self._resource_cache:
            if resource_id in self._resource_cache[resource_type]:
                resource = self._resource_cache[resource_type][resource_id]
                org_id = resource.get("organization")
                if org_id:
                    return await self._get_org_name(org_id)

        try:
            endpoint = f"{resource_type}/{resource_id}/"
            resource = await self.client.get(endpoint)

            if resource_type not in self._resource_cache:
                self._resource_cache[resource_type] = {}
            self._resource_cache[resource_type][resource_id] = resource

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

    async def _get_resource_name(
        self,
        resource_type: str | None,
        resource_id: int,
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
