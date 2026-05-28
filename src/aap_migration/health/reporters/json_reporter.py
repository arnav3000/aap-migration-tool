"""JSON health check reporter."""

import json
from typing import Any

from aap_migration.health.models import HealthCheckReport


class JSONReporter:
    """Generate JSON health check reports."""

    @staticmethod
    def generate(report: HealthCheckReport) -> str:
        """Generate JSON report.

        Args:
            report: Health check report

        Returns:
            JSON string
        """
        report_dict = JSONReporter._report_to_dict(report)
        return json.dumps(report_dict, indent=2, default=str)

    @staticmethod
    def _report_to_dict(report: HealthCheckReport) -> dict[str, Any]:
        """Convert report to dictionary.

        Args:
            report: Health check report

        Returns:
            Dictionary representation
        """
        return {
            "source_url": report.source_url,
            "timestamp": report.timestamp.isoformat(),
            "summary": report.summary,
            "migration_readiness": report.migration_readiness,
            "is_migration_ready": report.is_migration_ready,
            "has_critical_issues": report.has_critical_issues,
            "has_warnings": report.has_warnings,
            "results": [
                {
                    "check_name": r.check_name,
                    "severity": r.severity.value,
                    "status": r.status.value,
                    "message": r.message,
                    "count": r.count,
                    "details": r.details,
                    "recommendation": r.recommendation,
                    "affected_resources": r.affected_resources,
                }
                for r in report.results
            ],
        }
