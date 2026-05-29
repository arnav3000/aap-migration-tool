"""Health checker orchestrator."""

from datetime import datetime, timezone
from typing import Any

import structlog

from aap_migration.client.aap_client import AAPClient
from aap_migration.health.base_check import BaseHealthCheck
from aap_migration.health.checks import (
    DuplicateCheck,
    InventorySourceValidationCheck,
    JobTemplateValidationCheck,
    OrphanedReferenceCheck,
    PendingDeletionCheck,
    PlaybookValidationCheck,
    ProjectValidationCheck,
    SCMSourceValidationCheck,
    ScheduleValidationCheck,
)
from aap_migration.health.models import HealthCheckReport, HealthCheckResult

logger = structlog.get_logger()


class HealthChecker:
    """Orchestrator for running health checks.

    Provides a shared resource cache so that multiple checks fetching the
    same resource type (e.g., job_templates, projects) only make one set
    of API calls instead of each check fetching independently.

    Performance impact at scale (200K+ objects, 1000+ orgs):
    - Without cache: ~10,308 API calls with ~1,485 wasted on duplicates
    - With cache: ~8,823 API calls (-14% reduction)
    """

    def __init__(self, client: AAPClient):
        """Initialize health checker.

        Args:
            client: AAP client for API calls
        """
        self.client = client

        # Shared resource cache across all checks in a single run.
        # Keyed by resource_type (e.g., "job_templates", "projects").
        # Populated on first fetch, reused by subsequent checks.
        self._resource_cache: dict[str, list[dict[str, Any]]] = {}

        # Register all available checks
        self.available_checks = {
            "pending_deletion": PendingDeletionCheck,
            "duplicates": DuplicateCheck,
            "orphaned_references": OrphanedReferenceCheck,
            "job_template_validation": JobTemplateValidationCheck,
            "project_validation": ProjectValidationCheck,
            "inventory_source_validation": InventorySourceValidationCheck,
            "schedule_validation": ScheduleValidationCheck,
            "playbook_validation": PlaybookValidationCheck,
            "scm_source_validation": SCMSourceValidationCheck,
        }

    async def get_cached_resources(
        self, resource_type: str
    ) -> list[dict[str, Any]]:
        """Fetch resources with caching across checks.

        On first call for a given resource_type, fetches all pages from the
        API and stores the results. Subsequent calls return the cached data
        without any API calls.

        Args:
            resource_type: Type of resource to fetch (e.g., "job_templates")

        Returns:
            List of resources (from cache if available)
        """
        if resource_type not in self._resource_cache:
            logger.info(
                "fetching_resources",
                resource_type=resource_type,
                source="cache_miss",
            )
            # Use BaseHealthCheck's direct fetch method via a temporary instance
            fetcher = _CacheFetcher(self.client)
            resources = await fetcher._fetch_resources_direct(resource_type)
            self._resource_cache[resource_type] = resources
            logger.info(
                "resources_cached",
                resource_type=resource_type,
                count=len(resources),
            )
        else:
            logger.debug(
                "using_cached_resources",
                resource_type=resource_type,
                count=len(self._resource_cache[resource_type]),
            )

        return self._resource_cache[resource_type]

    def clear_cache(self) -> None:
        """Clear the shared resource cache.

        Useful between runs or when resource data may have changed.
        """
        cache_size = len(self._resource_cache)
        self._resource_cache.clear()
        logger.debug("resource_cache_cleared", previous_entries=cache_size)

    async def run_all_checks(self) -> HealthCheckReport:
        """Run all available health checks.

        Returns:
            HealthCheckReport with all results
        """
        return await self.run_checks(list(self.available_checks.keys()))

    async def run_checks(self, check_names: list[str]) -> HealthCheckReport:
        """Run specified health checks.

        Each check receives a reference to this checker so it can use the
        shared resource cache via get_cached_resources().

        Args:
            check_names: List of check names to run

        Returns:
            HealthCheckReport with results
        """
        logger.info(
            "health_check_started",
            checks=check_names,
            source_url=self.client.base_url,
        )

        # Clear cache at the start of each run to ensure fresh data
        self.clear_cache()

        results = []

        for check_name in check_names:
            if check_name not in self.available_checks:
                logger.warning(
                    "health_check_not_found",
                    check_name=check_name,
                    available=list(self.available_checks.keys()),
                )
                continue

            try:
                # Instantiate check with checker reference for cache access
                check_class = self.available_checks[check_name]
                check = check_class(self.client, checker=self)

                logger.info("health_check_running", check=check_name)
                result = await check.run()
                results.append(result)

                logger.info(
                    "health_check_completed",
                    check=check_name,
                    status=result.status.value,
                    severity=result.severity.value,
                    count=result.count,
                )

            except Exception as e:
                logger.error(
                    "health_check_error",
                    check=check_name,
                    error=str(e),
                    exc_info=True,
                )
                # Add error result
                from aap_migration.health.models import CheckStatus, Severity

                results.append(
                    HealthCheckResult(
                        check_name=check_name,
                        severity=Severity.INFO,
                        status=CheckStatus.ERROR,
                        message=f"Check failed with error: {str(e)}",
                        details={"error": str(e)},
                        recommendation="Check logs for details",
                        affected_resources=[],
                        count=0,
                    )
                )

        # Build report
        report = HealthCheckReport(
            source_url=self.client.base_url,
            timestamp=datetime.now(timezone.utc),
            results=results,
        )

        logger.info(
            "health_check_report_generated",
            total_checks=len(results),
            critical_issues=report.summary["critical"],
            warnings=report.summary["warning"],
            migration_readiness=f"{report.migration_readiness:.1f}%",
            cache_entries=len(self._resource_cache),
        )

        return report

    def has_critical_issues(self, report: HealthCheckReport) -> bool:
        """Check if report has any critical issues.

        Args:
            report: Health check report

        Returns:
            True if critical issues found, False otherwise
        """
        return report.has_critical_issues

    def get_check_names(self) -> list[str]:
        """Get list of available check names.

        Returns:
            List of check names
        """
        return list(self.available_checks.keys())


class _CacheFetcher(BaseHealthCheck):
    """Internal helper to access BaseHealthCheck's _fetch_resources_direct.

    Not a real health check -- only used by HealthChecker.get_cached_resources()
    to reuse the pagination logic in BaseHealthCheck._fetch_resources_direct().
    """

    @property
    def check_name(self) -> str:
        return "_cache_fetcher"

    @property
    def description(self) -> str:
        return "Internal cache fetcher"

    async def run(self) -> HealthCheckResult:
        raise NotImplementedError("_CacheFetcher is not a runnable check")
