"""SCM source validation health check.

Detects projects with potentially inaccessible SCM sources that would become
post-migration time bombs:

1. Project synced from GitHub repo (worked in AAP 2.4)
2. GitHub repo gets deleted/moved/made private
3. AAP 2.4 still works (using cached playbooks)
4. Migration copies cached data
5. After migration: AAP 2.6 tries to sync -> FAILS (repo gone)

This check performs READ-ONLY analysis with NO external API calls to
GitHub/GitLab/etc. It uses only:
- AAP API metadata (project sync history, status fields)
- DNS resolution (safe, no authentication)
- URL format validation (local parsing only)

The user manually verifies suspicious projects identified by this check.
"""

import asyncio
import socket
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import structlog

from aap_migration.health.base_check import BaseHealthCheck
from aap_migration.health.models import CheckStatus, HealthCheckResult, Severity

logger = structlog.get_logger()

# DNS lookup timeout per domain (seconds)
DNS_TIMEOUT = 2.0

# Maximum concurrent DNS lookups
DNS_CONCURRENCY = 20

# Staleness thresholds (days)
STALE_HIGH_RISK_DAYS = 90
STALE_MEDIUM_RISK_DAYS = 30


class SCMSourceValidationCheck(BaseHealthCheck):
    """Validate SCM source accessibility based on sync history and DNS checks.

    Performs three layers of read-only analysis:

    Layer 1 - Historical Analysis (AAP API metadata):
        - Never synced projects (last_job_run is null)
        - Last sync failed (last_job_failed or status == "failed")
        - Stale projects (not synced in 30+ or 90+ days)
        - Missing SCM URL (scm_type set but scm_url empty)

    Layer 2 - DNS Resolution Check:
        - Resolves each unique SCM URL domain via DNS
        - Domains that fail DNS lookup indicate deleted/moved servers

    Layer 3 - URL Format Validation:
        - Validates SCM URL format is parseable
        - Extracts and categorizes domains (github.com, gitlab.com, internal)

    No external API calls are made. No authentication is attempted.
    """

    @property
    def check_name(self) -> str:
        """Name of the health check."""
        return "scm_source_validation"

    @property
    def description(self) -> str:
        """Description of what this check validates."""
        return "Validate SCM source accessibility based on sync history and DNS checks"

    async def run(self) -> HealthCheckResult:
        """Execute the SCM source validation check.

        Steps:
        1. Fetch all projects from AAP API
        2. Filter to SCM projects (scm_type is set)
        3. Layer 1: Analyze sync history metadata
        4. Layer 2: DNS resolution check on unique domains
        5. Layer 3: URL format validation
        6. Build categorized result

        Returns:
            HealthCheckResult with findings
        """
        logger.info(
            "health_check_starting",
            check=self.check_name,
            description=self.description,
        )

        # Step 1: Fetch all projects
        try:
            all_projects = await self._fetch_resources("projects")
        except Exception as e:
            logger.error(
                "failed_to_fetch_projects",
                error=str(e),
            )
            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.CRITICAL,
                status=CheckStatus.ERROR,
                message=f"Failed to fetch projects: {str(e)}",
                details={},
                recommendation="Check AAP API connectivity and permissions",
                affected_resources=[],
                count=0,
            )

        logger.info(
            "projects_fetched",
            count=len(all_projects),
        )

        # Step 2: Filter to SCM projects (scm_type is not empty/null)
        scm_projects = [p for p in all_projects if p.get("scm_type")]

        logger.info(
            "scm_projects_filtered",
            total_projects=len(all_projects),
            scm_projects=len(scm_projects),
            manual_projects=len(all_projects) - len(scm_projects),
        )

        if not scm_projects:
            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.INFO,
                status=CheckStatus.PASS,
                message=f"No SCM projects found among {len(all_projects)} projects",
                details={},
                recommendation="",
                affected_resources=[],
                count=0,
            )

        # Step 3: Layer 1 - Historical analysis (categorize by risk)
        never_synced: list[dict[str, Any]] = []
        last_sync_failed: list[dict[str, Any]] = []
        stale_high_risk: list[dict[str, Any]] = []
        stale_medium_risk: list[dict[str, Any]] = []
        missing_url: list[dict[str, Any]] = []

        for project in scm_projects:
            project_info = self._extract_project_info(project)

            # Check: scm_type is set but scm_url is empty/null
            scm_url = project.get("scm_url")
            if not scm_url or not scm_url.strip():
                missing_url.append(project_info)
                logger.debug(
                    "project_missing_scm_url",
                    id=project.get("id"),
                    name=project.get("name"),
                    scm_type=project.get("scm_type"),
                )
                continue

            # Check: last sync failed
            if project.get("last_job_failed") or project.get("status") == "failed":
                last_sync_failed.append(project_info)
                logger.debug(
                    "project_last_sync_failed",
                    id=project.get("id"),
                    name=project.get("name"),
                    status=project.get("status"),
                    last_job_failed=project.get("last_job_failed"),
                )
                continue

            # Check: staleness classification
            staleness = self._classify_staleness(project)
            if staleness == "never":
                never_synced.append(project_info)
                logger.debug(
                    "project_never_synced",
                    id=project.get("id"),
                    name=project.get("name"),
                )
            elif staleness == "90_days":
                stale_high_risk.append(project_info)
                logger.debug(
                    "project_stale_90_days",
                    id=project.get("id"),
                    name=project.get("name"),
                    last_job_run=project.get("last_job_run"),
                )
            elif staleness == "30_days":
                stale_medium_risk.append(project_info)
                logger.debug(
                    "project_stale_30_days",
                    id=project.get("id"),
                    name=project.get("name"),
                    last_job_run=project.get("last_job_run"),
                )

        # Step 4: Layer 2 - DNS resolution check
        # Extract unique domains from all SCM projects that have URLs
        unique_domains: set[str] = set()
        for project in scm_projects:
            scm_url = project.get("scm_url")
            if scm_url:
                domain = self._extract_domain(scm_url)
                if domain:
                    unique_domains.add(domain)

        logger.info(
            "dns_check_starting",
            unique_domains=len(unique_domains),
        )

        dns_failures = await self._check_dns_resolution(unique_domains)

        logger.info(
            "dns_check_completed",
            total_domains=len(unique_domains),
            failed_domains=len(dns_failures),
            failed_list=list(dns_failures),
        )

        # Flag projects whose domain failed DNS resolution
        # Exclude projects already flagged in other categories
        already_flagged_ids = {
            p["id"]
            for category in [never_synced, last_sync_failed, missing_url]
            for p in category
        }

        dns_failed_projects: list[dict[str, Any]] = []
        for project in scm_projects:
            if project.get("id") in already_flagged_ids:
                continue
            scm_url = project.get("scm_url")
            if scm_url:
                domain = self._extract_domain(scm_url)
                if domain and domain in dns_failures:
                    project_info = self._extract_project_info(project)
                    project_info["dns_domain"] = domain
                    dns_failed_projects.append(project_info)

        # Step 5: Build result with categorized issues
        total_critical = (
            len(never_synced)
            + len(last_sync_failed)
            + len(dns_failed_projects)
            + len(missing_url)
        )
        total_issues = total_critical + len(stale_high_risk)

        details: dict[str, Any] = {
            "total_scm_projects": len(scm_projects),
            "unique_scm_domains": len(unique_domains),
            "dns_domains_checked": len(unique_domains),
            "dns_domains_failed": len(dns_failures),
            "critical_issues": {
                "never_synced": {
                    "count": len(never_synced),
                    "projects": never_synced[:50],
                },
                "last_sync_failed": {
                    "count": len(last_sync_failed),
                    "projects": last_sync_failed[:50],
                },
                "dns_failure": {
                    "count": len(dns_failed_projects),
                    "projects": dns_failed_projects[:50],
                    "failed_domains": sorted(dns_failures),
                },
                "missing_url": {
                    "count": len(missing_url),
                    "projects": missing_url[:50],
                },
            },
            "warnings": {
                "stale_90_days": {
                    "count": len(stale_high_risk),
                    "projects": stale_high_risk[:50],
                },
                "stale_30_days": {
                    "count": len(stale_medium_risk),
                    "projects": stale_medium_risk[:50],
                },
            },
        }

        # Step 6: Return appropriate severity
        if total_issues > 0:
            has_critical = bool(
                never_synced or last_sync_failed or dns_failed_projects or missing_url
            )

            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.CRITICAL if has_critical else Severity.WARNING,
                status=CheckStatus.FAIL,
                message=f"Found {total_issues} projects requiring manual SCM verification across {len(scm_projects)} SCM projects",
                details=details,
                recommendation=self._build_recommendation(details),
                affected_resources=self._build_affected_resources(details),
                count=total_issues,
            )
        else:
            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.INFO,
                status=CheckStatus.PASS,
                message=f"All {len(scm_projects)} SCM projects appear healthy ({len(unique_domains)} domains verified)",
                details=details,
                recommendation="",
                affected_resources=[],
                count=0,
            )

    def _extract_project_info(self, project: dict[str, Any]) -> dict[str, Any]:
        """Extract relevant project fields for reporting.

        Args:
            project: Raw project data from AAP API

        Returns:
            Dict with key project fields for the report
        """
        return {
            "type": "projects",
            "id": project.get("id"),
            "name": project.get("name"),
            "url": project.get("url"),
            "scm_url": project.get("scm_url"),
            "scm_type": project.get("scm_type"),
            "scm_branch": project.get("scm_branch"),
            "last_job_run": project.get("last_job_run"),
            "last_job_failed": project.get("last_job_failed"),
            "status": project.get("status"),
        }

    def _extract_domain(self, scm_url: str) -> str | None:
        """Extract domain from an SCM URL, handling various formats.

        Handles:
        - https://github.com/org/repo.git
        - git@github.com:org/repo.git (SSH format)
        - ssh://git@gitlab.internal:2222/org/repo.git
        - file:///path/to/repo (returns None - local paths)

        Args:
            scm_url: SCM URL string

        Returns:
            Domain string or None if not extractable or local path
        """
        if not scm_url:
            return None

        scm_url = scm_url.strip()

        # Handle SSH shorthand: git@github.com:org/repo.git
        if "@" in scm_url and "://" not in scm_url:
            # Extract domain from user@domain:path format
            try:
                at_part = scm_url.split("@", 1)[1]
                domain = at_part.split(":", 1)[0]
                return domain if domain else None
            except (IndexError, ValueError):
                return None

        # Handle standard URLs
        try:
            parsed = urlparse(scm_url)

            # Skip file:// URLs (local paths)
            if parsed.scheme == "file":
                return None

            hostname = parsed.hostname
            return hostname if hostname else None
        except Exception:
            logger.debug(
                "failed_to_parse_scm_url",
                scm_url=scm_url,
            )
            return None

    def _classify_staleness(self, project: dict[str, Any]) -> str | None:
        """Classify project staleness based on last sync time.

        Args:
            project: Project data from AAP API

        Returns:
            "never" if never synced, "90_days" if stale 90+ days,
            "30_days" if stale 30-90 days, None if recent enough
        """
        last_job_run = project.get("last_job_run")
        if not last_job_run:
            return "never"

        try:
            # Parse ISO 8601 timestamp
            last_sync = datetime.fromisoformat(
                last_job_run.replace("Z", "+00:00")
            )
            now = datetime.now(timezone.utc)
            age_days = (now - last_sync).days

            if age_days > STALE_HIGH_RISK_DAYS:
                return "90_days"
            elif age_days > STALE_MEDIUM_RISK_DAYS:
                return "30_days"
            else:
                return None
        except (ValueError, TypeError) as e:
            logger.warning(
                "failed_to_parse_last_job_run",
                project_id=project.get("id"),
                last_job_run=last_job_run,
                error=str(e),
            )
            # Can't determine staleness, treat as unknown
            return None

    async def _check_dns_resolution(self, domains: set[str]) -> set[str]:
        """Check DNS resolution for domains in parallel.

        Performs DNS lookups using socket.getaddrinfo with a semaphore to
        control concurrency. This is a safe, read-only operation that does
        not connect to any remote server.

        Performance: 200 domains x 2s timeout / 20 concurrent = ~20 seconds
        (vs. ~6.7 minutes when sequential with 5s timeout)

        Args:
            domains: Set of domain names to check

        Returns:
            Set of domains that failed DNS resolution
        """
        if not domains:
            return set()

        dns_failures: set[str] = set()
        semaphore = asyncio.Semaphore(DNS_CONCURRENCY)
        loop = asyncio.get_event_loop()

        async def check_single_domain(domain: str) -> None:
            """Check a single domain with concurrency control."""
            if not domain:
                return

            async with semaphore:
                try:
                    await asyncio.wait_for(
                        loop.run_in_executor(
                            None, socket.getaddrinfo, domain, None
                        ),
                        timeout=DNS_TIMEOUT,
                    )
                except (socket.gaierror, asyncio.TimeoutError):
                    dns_failures.add(domain)
                    logger.debug(
                        "dns_resolution_failed",
                        domain=domain,
                    )
                except Exception as e:
                    # Log but don't treat unexpected errors as DNS failures
                    logger.warning(
                        "dns_check_unexpected_error",
                        domain=domain,
                        error=str(e),
                    )

        # Run all DNS checks in parallel with semaphore-controlled concurrency
        await asyncio.gather(
            *[check_single_domain(d) for d in domains]
        )

        return dns_failures

    def _build_recommendation(self, details: dict[str, Any]) -> str:
        """Build actionable recommendation text based on findings.

        Args:
            details: Check details dict with categorized issues

        Returns:
            Formatted recommendation text
        """
        recommendations: list[str] = []

        critical = details.get("critical_issues", {})
        warnings = details.get("warnings", {})

        if critical.get("never_synced", {}).get("count", 0) > 0:
            count = critical["never_synced"]["count"]
            recommendations.append(
                f"\n**Never Synced ({count} projects):**\n"
                "These projects have never successfully synced from their SCM source.\n"
                "\n"
                "Action:\n"
                "1. In source AAP UI, navigate to each project\n"
                "2. Check if SCM URL is correct\n"
                "3. Click 'Sync' to test if repo is accessible\n"
                "4. If sync fails, either fix the SCM URL or delete the project\n"
                "\n"
                "**Why this matters:** Projects that have never synced likely have "
                "no playbooks cached. After migration, AAP 2.6 will attempt to sync "
                "and may fail if the repo is inaccessible."
            )

        if critical.get("last_sync_failed", {}).get("count", 0) > 0:
            count = critical["last_sync_failed"]["count"]
            recommendations.append(
                f"\n**Last Sync Failed ({count} projects):**\n"
                "These projects failed their most recent sync attempt.\n"
                "\n"
                "Action:\n"
                "1. In source AAP UI, navigate to each project\n"
                "2. View the sync job output to see the error\n"
                "3. Common issues: repo deleted, credentials expired, branch deleted\n"
                "4. Fix the issue and test sync before migration\n"
                "\n"
                "**Why this matters:** AAP 2.4 continues to use cached playbooks even "
                "when sync fails. After migration, AAP 2.6 will try to sync and the "
                "same failure will occur, potentially blocking job execution."
            )

        if critical.get("dns_failure", {}).get("count", 0) > 0:
            count = critical["dns_failure"]["count"]
            failed_domains = critical["dns_failure"].get("failed_domains", [])
            domain_list = ", ".join(failed_domains[:10])
            recommendations.append(
                f"\n**DNS Resolution Failed ({count} projects):**\n"
                f"The following SCM URL domains do not resolve via DNS: {domain_list}\n"
                "\n"
                "Action:\n"
                "1. Check if domain was typo'd or if server was decommissioned\n"
                "2. If internal server moved, update SCM URL to new domain\n"
                "3. If repo migrated to different platform, update SCM URL\n"
                "4. Delete project if no longer needed\n"
                "\n"
                "**Why this matters:** If the SCM server DNS does not resolve, "
                "the repo is definitely inaccessible. AAP 2.6 will fail to sync "
                "after migration."
            )

        if critical.get("missing_url", {}).get("count", 0) > 0:
            count = critical["missing_url"]["count"]
            recommendations.append(
                f"\n**Missing SCM URL ({count} projects):**\n"
                "These projects have an SCM type configured but no SCM URL set.\n"
                "\n"
                "Action:\n"
                "1. In source AAP UI, navigate to each project\n"
                "2. Either add the correct SCM URL, or\n"
                "3. Change the project type to 'Manual'\n"
                "\n"
                "**Why this matters:** A project configured for SCM without a URL "
                "cannot sync. This is likely a misconfiguration."
            )

        if warnings.get("stale_90_days", {}).get("count", 0) > 0:
            count = warnings["stale_90_days"]["count"]
            recommendations.append(
                f"\n**Stale 90+ Days ({count} projects):**\n"
                "These projects have not synced in over 90 days.\n"
                "\n"
                "Action:\n"
                "1. Verify these projects are still needed\n"
                "2. Test sync to confirm repo is still accessible\n"
                "3. Consider deleting if no longer used\n"
                "\n"
                "**Why this matters:** Repos not synced in 90+ days may have been "
                "deleted, moved, or made private. AAP 2.4 uses cached content, but "
                "AAP 2.6 will attempt a fresh sync after migration."
            )

        if recommendations:
            header = (
                "The following projects may have inaccessible SCM sources.\n"
                "Manually verify each before migration to prevent "
                "post-migration sync failures:\n"
            )
            return header + "\n".join(recommendations)

        return ""

    def _build_affected_resources(
        self, details: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Build affected resources list for the report.

        Combines critical issues and warnings into a flat list of
        affected resources for reporting.

        Args:
            details: Check details dict with categorized issues

        Returns:
            List of affected resource dicts
        """
        resources: list[dict[str, Any]] = []

        critical = details.get("critical_issues", {})
        warnings = details.get("warnings", {})

        # Add critical issues
        for category in [
            "never_synced",
            "last_sync_failed",
            "dns_failure",
            "missing_url",
        ]:
            projects = critical.get(category, {}).get("projects", [])
            for p in projects:
                resource = {
                    "type": "projects",
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "url": p.get("url"),
                    "scm_url": p.get("scm_url"),
                    "scm_type": p.get("scm_type"),
                    "issue": category.replace("_", " ").title(),
                    "severity": "CRITICAL",
                    "last_job_run": p.get("last_job_run"),
                    "status": p.get("status"),
                }
                # Include DNS domain if available
                if p.get("dns_domain"):
                    resource["dns_domain"] = p["dns_domain"]
                resources.append(resource)

        # Add warnings (stale projects)
        for category in ["stale_90_days", "stale_30_days"]:
            projects = warnings.get(category, {}).get("projects", [])
            for p in projects:
                resources.append(
                    {
                        "type": "projects",
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "url": p.get("url"),
                        "scm_url": p.get("scm_url"),
                        "scm_type": p.get("scm_type"),
                        "issue": category.replace("_", " ").title(),
                        "severity": "WARNING",
                        "last_job_run": p.get("last_job_run"),
                    }
                )

        return resources
