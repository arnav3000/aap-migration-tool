"""Health checker orchestrator."""

from datetime import datetime, timezone
from typing import Any

import structlog

from aap_migration.client.aap_client import AAPClient
from aap_migration.health.checks import (
    DuplicateCheck,
    InventorySourceValidationCheck,
    JobTemplateValidationCheck,
    OrphanedReferenceCheck,
    PendingDeletionCheck,
    ProjectValidationCheck,
    ScheduleValidationCheck,
)
from aap_migration.health.models import HealthCheckReport, HealthCheckResult

logger = structlog.get_logger()


class HealthChecker:
    """Orchestrator for running health checks."""

    def __init__(self, client: AAPClient):
        """Initialize health checker.

        Args:
            client: AAP client for API calls
        """
        self.client = client

        # Register all available checks
        self.available_checks = {
            "pending_deletion": PendingDeletionCheck,
            "duplicates": DuplicateCheck,
            "orphaned_references": OrphanedReferenceCheck,
            "job_template_validation": JobTemplateValidationCheck,
            "project_validation": ProjectValidationCheck,
            "inventory_source_validation": InventorySourceValidationCheck,
            "schedule_validation": ScheduleValidationCheck,
        }

    async def run_all_checks(self) -> HealthCheckReport:
        """Run all available health checks.

        Returns:
            HealthCheckReport with all results
        """
        return await self.run_checks(list(self.available_checks.keys()))

    async def run_checks(self, check_names: list[str]) -> HealthCheckReport:
        """Run specified health checks.

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
                # Instantiate and run check
                check_class = self.available_checks[check_name]
                check = check_class(self.client)

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
