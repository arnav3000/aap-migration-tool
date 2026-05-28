"""Job template validation health check."""

import asyncio
from typing import Any

import structlog

from aap_migration.health.base_check import BaseHealthCheck
from aap_migration.health.models import CheckStatus, HealthCheckResult, Severity

logger = structlog.get_logger()


class JobTemplateValidationCheck(BaseHealthCheck):
    """Validate job template configuration issues that cause migration failures.

    Based on real customer data analysis:
    - 171 failures: Missing project assignment
    - 112 failures: Inventory prompt mismatch
    - Total: 283 preventable failures (6.5% of all job templates)
    """

    @property
    def check_name(self) -> str:
        """Name of the health check."""
        return "job_template_validation"

    @property
    def description(self) -> str:
        """Description of what this check validates."""
        return "Validate job template field configurations (project, inventory, prompts)"

    async def run(self) -> HealthCheckResult:
        """Execute the job template validation check.

        Returns:
            HealthCheckResult with findings
        """
        logger.info(
            "health_check_starting",
            check=self.check_name,
            description=self.description,
        )

        # Fetch all job templates
        try:
            job_templates = await self._fetch_resources("job_templates")
        except Exception as e:
            logger.error(
                "failed_to_fetch_job_templates",
                error=str(e),
            )
            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.CRITICAL,
                status=CheckStatus.ERROR,
                message=f"Failed to fetch job templates: {str(e)}",
                details={},
                recommendation="Check AAP API connectivity and permissions",
                affected_resources=[],
                count=0,
            )

        logger.info(
            "job_templates_fetched",
            count=len(job_templates),
        )

        # Track issues
        missing_project = []
        inventory_prompt_mismatch = []

        # Validate each job template
        for jt in job_templates:
            # Check 1: Missing project assignment
            if jt.get("project") is None:
                missing_project.append(
                    {
                        "type": "job_templates",
                        "id": jt.get("id"),
                        "name": jt.get("name"),
                        "url": jt.get("url"),
                        "issue": "Missing project assignment",
                    }
                )
                logger.debug(
                    "job_template_missing_project",
                    id=jt.get("id"),
                    name=jt.get("name"),
                )

            # Check 2: Inventory prompt mismatch
            # AAP 2.6 requires either:
            # - inventory is set (has default)
            # - OR ask_inventory_on_launch is true (prompts for it)
            has_inventory = jt.get("inventory") is not None
            asks_for_inventory = jt.get("ask_inventory_on_launch", False)

            if not has_inventory and not asks_for_inventory:
                inventory_prompt_mismatch.append(
                    {
                        "type": "job_templates",
                        "id": jt.get("id"),
                        "name": jt.get("name"),
                        "url": jt.get("url"),
                        "issue": "No inventory and not configured to prompt on launch",
                        "inventory": None,
                        "ask_inventory_on_launch": asks_for_inventory,
                    }
                )
                logger.debug(
                    "job_template_inventory_prompt_mismatch",
                    id=jt.get("id"),
                    name=jt.get("name"),
                )

        # Calculate totals
        total_issues = len(missing_project) + len(inventory_prompt_mismatch)

        # Build result
        if total_issues > 0:
            details = {}
            if missing_project:
                details["missing_project"] = {
                    "count": len(missing_project),
                    "description": "Job templates without project assignment",
                    "error_on_migration": "Job Templates must have a project assigned.",
                }
            if inventory_prompt_mismatch:
                details["inventory_prompt_mismatch"] = {
                    "count": len(inventory_prompt_mismatch),
                    "description": "Job templates without inventory and not prompting on launch",
                    "error_on_migration": "You must either set a default value or ask to prompt on launch.",
                }

            message = f"Found {total_issues} job template configuration issues across {len(job_templates)} templates"

            recommendation = self._build_recommendation(
                len(missing_project),
                len(inventory_prompt_mismatch),
            )

            affected_resources = missing_project + inventory_prompt_mismatch

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
                message=f"All {len(job_templates)} job templates have valid configuration",
                details={},
                recommendation="",
                affected_resources=[],
                count=0,
            )

    def _build_recommendation(
        self,
        missing_project_count: int,
        inventory_mismatch_count: int,
    ) -> str:
        """Build remediation recommendation based on issues found.

        Args:
            missing_project_count: Number of templates missing project
            inventory_mismatch_count: Number with inventory prompt issues

        Returns:
            Formatted recommendation text
        """
        recommendations = []

        if missing_project_count > 0:
            recommendations.append(
                f"\n**Missing Project Assignment ({missing_project_count} templates):**\n"
                "1. In source AAP UI, navigate to each affected job template\n"
                "2. Assign a project in the Project field\n"
                "3. Save the template\n"
                "\n"
                "**Why this fails:** AAP 2.6 requires all job templates to have a project. "
                "These templates are orphaned (project was deleted or never set)."
            )

        if inventory_mismatch_count > 0:
            recommendations.append(
                f"\n**Inventory Prompt Mismatch ({inventory_mismatch_count} templates):**\n"
                "1. In source AAP UI, navigate to each affected job template\n"
                "2. Either:\n"
                "   - Set a default Inventory, OR\n"
                "   - Enable 'Prompt on Launch' for Inventory field\n"
                "3. Save the template\n"
                "\n"
                "**Why this fails:** AAP 2.6 requires job templates to either have a default "
                "inventory OR be configured to prompt for it at launch time. Templates with "
                "neither will fail to import."
            )

        if recommendations:
            header = (
                "Fix the following job template issues in source AAP before migration:\n"
            )
            return header + "\n".join(recommendations)
        else:
            return ""
