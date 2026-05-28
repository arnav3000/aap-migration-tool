"""Health check data models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Health check severity levels."""

    CRITICAL = "CRITICAL"  # Must fix before migration
    WARNING = "WARNING"  # Should fix, migration may partially fail
    INFO = "INFO"  # Best practices, optimization
    PASS = "PASS"  # Check passed


class CheckStatus(str, Enum):
    """Health check status."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    ERROR = "ERROR"


@dataclass
class HealthCheckResult:
    """Result of a single health check."""

    check_name: str
    severity: Severity
    status: CheckStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    recommendation: str = ""
    affected_resources: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0

    @property
    def is_critical(self) -> bool:
        """Check if result is critical."""
        return self.severity == Severity.CRITICAL and self.status == CheckStatus.FAIL

    @property
    def is_warning(self) -> bool:
        """Check if result is a warning."""
        return self.severity == Severity.WARNING and self.status == CheckStatus.FAIL

    @property
    def is_pass(self) -> bool:
        """Check if result passed."""
        return self.status == CheckStatus.PASS


@dataclass
class HealthCheckReport:
    """Complete health check report."""

    source_url: str
    timestamp: datetime
    results: list[HealthCheckResult]
    summary: dict[str, int] = field(default_factory=dict)
    migration_readiness: float = 0.0

    def __post_init__(self):
        """Calculate summary statistics."""
        self.summary = {
            "total_checks": len(self.results),
            "passed": sum(1 for r in self.results if r.is_pass),
            "critical": sum(1 for r in self.results if r.is_critical),
            "warning": sum(1 for r in self.results if r.is_warning),
            "info": sum(1 for r in self.results if r.severity == Severity.INFO and r.status == CheckStatus.FAIL),
        }

        # Calculate migration readiness (0-100%)
        # Critical issues severely impact readiness
        total = len(self.results)
        if total > 0:
            passed = self.summary["passed"]
            critical = self.summary["critical"]
            warning = self.summary["warning"]

            # Critical failures reduce readiness by 20% each
            # Warnings reduce readiness by 5% each
            critical_penalty = min(critical * 20, 100)
            warning_penalty = min(warning * 5, 50)

            self.migration_readiness = max(0, 100 - critical_penalty - warning_penalty)

    @property
    def has_critical_issues(self) -> bool:
        """Check if report has any critical issues."""
        return self.summary.get("critical", 0) > 0

    @property
    def has_warnings(self) -> bool:
        """Check if report has any warnings."""
        return self.summary.get("warning", 0) > 0

    @property
    def is_migration_ready(self) -> bool:
        """Check if migration is ready (no critical issues)."""
        return not self.has_critical_issues
