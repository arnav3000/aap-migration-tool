"""Project validation health check."""

import asyncio
from typing import Any

import structlog

from aap_migration.health.base_check import BaseHealthCheck
from aap_migration.health.models import CheckStatus, HealthCheckResult, Severity

logger = structlog.get_logger()


class ProjectValidationCheck(BaseHealthCheck):
    """Validate project configuration issues that cause migration failures.

    Based on real customer data analysis:
    - 5 failures: SCM options set on manual projects
    - 4 failures: Missing organization assignment
    - Total: 9 preventable failures (<1% of all projects)
    """

    @property
    def check_name(self) -> str:
        """Name of the health check."""
        return "project_validation"

    @property
    def description(self) -> str:
        """Description of what this check validates."""
        return "Validate project field configurations (organization, SCM options)"

    async def run(self) -> HealthCheckResult:
        """Execute the project validation check.

        Returns:
            HealthCheckResult with findings
        """
        logger.info(
            "health_check_starting",
            check=self.check_name,
            description=self.description,
        )

        # Fetch all projects
        try:
            projects = await self._fetch_resources("projects")
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
            count=len(projects),
        )

        # Track issues
        manual_with_scm_options = []
        missing_organization = []

        # Validate each project
        for project in projects:
            # Check 1: Manual projects with SCM options
            # Manual projects have scm_type empty or null
            is_manual = not project.get("scm_type")

            if is_manual:
                # SCM options that must be false for manual projects
                scm_options = {
                    "scm_track_submodules": project.get("scm_track_submodules"),
                    "scm_clean": project.get("scm_clean"),
                    "scm_delete_on_update": project.get("scm_delete_on_update"),
                    "scm_update_on_launch": project.get("scm_update_on_launch"),
                }

                # Find options that are set to true
                invalid_options = [
                    opt for opt, value in scm_options.items() if value
                ]

                if invalid_options:
                    manual_with_scm_options.append(
                        {
                            "type": "projects",
                            "id": project.get("id"),
                            "name": project.get("name"),
                            "url": project.get("url"),
                            "issue": "Manual project with SCM options enabled",
                            "scm_type": project.get("scm_type"),
                            "invalid_options": invalid_options,
                        }
                    )
                    logger.debug(
                        "project_manual_with_scm_options",
                        id=project.get("id"),
                        name=project.get("name"),
                        options=invalid_options,
                    )

            # Check 2: Missing organization
            if project.get("organization") is None:
                missing_organization.append(
                    {
                        "type": "projects",
                        "id": project.get("id"),
                        "name": project.get("name"),
                        "url": project.get("url"),
                        "issue": "Missing organization assignment",
                    }
                )
                logger.debug(
                    "project_missing_organization",
                    id=project.get("id"),
                    name=project.get("name"),
                )

        # Calculate totals
        total_issues = len(manual_with_scm_options) + len(missing_organization)

        # Build result
        if total_issues > 0:
            details = {}
            if manual_with_scm_options:
                details["manual_with_scm_options"] = {
                    "count": len(manual_with_scm_options),
                    "description": "Manual projects with SCM update options enabled",
                    "error_on_migration": "Update options must be set to false for manual projects.",
                }
            if missing_organization:
                details["missing_organization"] = {
                    "count": len(missing_organization),
                    "description": "Projects without organization assignment",
                    "error_on_migration": "Projects must have an organization assigned.",
                }

            message = f"Found {total_issues} project configuration issues across {len(projects)} projects"

            recommendation = self._build_recommendation(
                len(manual_with_scm_options),
                len(missing_organization),
            )

            affected_resources = manual_with_scm_options + missing_organization

            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.CRITICAL,
                status=CheckStatus.FAIL,
                message=message,
                details=details,
                recommendation=recommendation,
                affected_resources=affected_resources,
                count=total_issues,
            )
        else:
            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.INFO,
                status=CheckStatus.PASS,
                message=f"All {len(projects)} projects have valid configuration",
                details={},
                recommendation="",
                affected_resources=[],
                count=0,
            )

    def _build_recommendation(
        self,
        manual_scm_count: int,
        missing_org_count: int,
    ) -> str:
        """Build remediation recommendation based on issues found.

        Args:
            manual_scm_count: Number of manual projects with SCM options
            missing_org_count: Number missing organization

        Returns:
            Formatted recommendation text
        """
        recommendations = []

        if manual_scm_count > 0:
            recommendations.append(
                f"\n**Manual Projects with SCM Options ({manual_scm_count} projects):**\n"
                "1. In source AAP UI, navigate to each affected project\n"
                "2. Uncheck the following SCM update options:\n"
                "   - Clean (scm_clean)\n"
                "   - Delete on Update (scm_delete_on_update)\n"
                "   - Update Revision on Launch (scm_update_on_launch)\n"
                "   - Track submodules (scm_track_submodules)\n"
                "3. Save the project\n"
                "\n"
                "**Why this fails:** AAP 2.6 validates that manual projects (no SCM configured) "
                "cannot have SCM update options enabled. These options only apply to Git/SVN projects."
            )

        if missing_org_count > 0:
            recommendations.append(
                f"\n**Missing Organization ({missing_org_count} projects):**\n"
                "1. In source AAP UI, navigate to each affected project\n"
                "2. Assign an organization in the Organization field\n"
                "3. Save the project\n"
                "\n"
                "**Why this fails:** AAP 2.6 requires all projects to belong to an organization. "
                "These are orphaned projects (organization was deleted or never set). "
                "If these projects are unused, consider deleting them instead."
            )

        if recommendations:
            header = (
                "Fix the following project issues in source AAP before migration:\n"
            )
            return header + "\n".join(recommendations)
        else:
            return ""
