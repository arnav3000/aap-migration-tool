"""Schedule validation health check."""

import asyncio
from typing import Any

import structlog

from aap_migration.health.base_check import BaseHealthCheck
from aap_migration.health.models import CheckStatus, HealthCheckResult, Severity

logger = structlog.get_logger()


class ScheduleValidationCheck(BaseHealthCheck):
    """Validate schedule configuration issues that cause migration failures.

    Based on real customer data analysis:
    - 30+ failures: Missing required variables (variables_needed_to_start)
    - 10+ failures: Extra data not allowed on launch
    - 10 failures: Database credential variable errors
    - 9 failures: Inventory prompt mismatch
    - 2 failures: Missing template inventory
    - Total: 60+ preventable failures (7% of all schedules)
    """

    @property
    def check_name(self) -> str:
        """Name of the health check."""
        return "schedule_validation"

    @property
    def description(self) -> str:
        """Description of what this check validates."""
        return "Validate schedule configurations (variables, prompts, credentials)"

    async def run(self) -> HealthCheckResult:
        """Execute the schedule validation check.

        Returns:
            HealthCheckResult with findings
        """
        logger.info(
            "health_check_starting",
            check=self.check_name,
            description=self.description,
        )

        # Fetch all schedules
        try:
            schedules = await self._fetch_resources("schedules")
        except Exception as e:
            logger.error(
                "failed_to_fetch_schedules",
                error=str(e),
            )
            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.CRITICAL,
                status=CheckStatus.ERROR,
                message=f"Failed to fetch schedules: {str(e)}",
                details={},
                recommendation="Check AAP API connectivity and permissions",
                affected_resources=[],
                count=0,
            )

        logger.info(
            "schedules_fetched",
            count=len(schedules),
        )

        # Fetch job templates and workflow templates for validation
        # We need these to check variables_needed_to_start and prompt settings
        try:
            job_templates = await self._fetch_resources("job_templates")
            workflow_templates = await self._fetch_resources("workflow_job_templates")
        except Exception as e:
            logger.error(
                "failed_to_fetch_templates",
                error=str(e),
            )
            return HealthCheckResult(
                check_name=self.check_name,
                severity=Severity.CRITICAL,
                status=CheckStatus.ERROR,
                message=f"Failed to fetch templates: {str(e)}",
                details={},
                recommendation="Check AAP API connectivity and permissions",
                affected_resources=[],
                count=0,
            )

        # Build template lookup (unified_job_template can be job or workflow)
        template_lookup = {}
        for jt in job_templates:
            template_lookup[jt["id"]] = {
                "type": "job_template",
                "data": jt,
            }
        for wt in workflow_templates:
            template_lookup[wt["id"]] = {
                "type": "workflow_job_template",
                "data": wt,
            }

        # Track issues
        extra_data_not_allowed = []
        inventory_not_allowed = []
        credential_variables = []
        missing_required_vars = []

        # Validate each schedule
        for schedule in schedules:
            ujt_id = schedule.get("unified_job_template")
            if not ujt_id:
                continue

            template_info = template_lookup.get(ujt_id)
            if not template_info:
                # This is an orphaned reference - will be caught by orphaned_references check
                continue

            template_type = template_info["type"]
            template = template_info["data"]

            # Workflow templates don't have these validation issues
            # (they don't have variables, inventory, etc.)
            if template_type != "job_template":
                continue

            # Check 1: Extra data provided but template doesn't allow it
            extra_data = schedule.get("extra_data", {})
            if extra_data:
                asks_for_variables = template.get("ask_variables_on_launch", False)
                if not asks_for_variables:
                    extra_data_not_allowed.append(
                        {
                            "type": "schedules",
                            "id": schedule.get("id"),
                            "name": schedule.get("name"),
                            "url": schedule.get("url"),
                            "issue": "Provides extra_data but template doesn't allow variables on launch",
                            "extra_data_vars": list(extra_data.keys()),
                            "template_id": ujt_id,
                            "template_name": template.get("name"),
                            "ask_variables_on_launch": asks_for_variables,
                        }
                    )
                    logger.debug(
                        "schedule_extra_data_not_allowed",
                        schedule_id=schedule.get("id"),
                        schedule_name=schedule.get("name"),
                        extra_vars=list(extra_data.keys()),
                    )

            # Check 2: Inventory specified but template doesn't allow it
            schedule_inventory = schedule.get("inventory")
            if schedule_inventory:
                asks_for_inventory = template.get("ask_inventory_on_launch", False)
                if not asks_for_inventory:
                    inventory_not_allowed.append(
                        {
                            "type": "schedules",
                            "id": schedule.get("id"),
                            "name": schedule.get("name"),
                            "url": schedule.get("url"),
                            "issue": "Specifies inventory but template doesn't allow inventory on launch",
                            "inventory": schedule_inventory,
                            "template_id": ujt_id,
                            "template_name": template.get("name"),
                            "ask_inventory_on_launch": asks_for_inventory,
                        }
                    )
                    logger.debug(
                        "schedule_inventory_not_allowed",
                        schedule_id=schedule.get("id"),
                        schedule_name=schedule.get("name"),
                    )

            # Check 3: Credential variables (pattern matching for likely credential injection vars)
            # These often fail with "has no database value to replace with"
            if extra_data:
                cred_vars = [
                    var
                    for var in extra_data.keys()
                    if (
                        var.endswith("pwd")
                        or var.endswith("password")
                        or "password" in var.lower()
                        or var.endswith("token")
                        or var.endswith("secret")
                    )
                ]
                if cred_vars:
                    credential_variables.append(
                        {
                            "type": "schedules",
                            "id": schedule.get("id"),
                            "name": schedule.get("name"),
                            "url": schedule.get("url"),
                            "issue": "Contains credential-like variables that may require validation",
                            "credential_vars": cred_vars,
                            "template_id": ujt_id,
                            "template_name": template.get("name"),
                        }
                    )
                    logger.debug(
                        "schedule_credential_variables",
                        schedule_id=schedule.get("id"),
                        schedule_name=schedule.get("name"),
                        cred_vars=cred_vars,
                    )

            # Check 4: Missing required variables (survey spec validation)
            # This is complex - job template may have a survey spec with required fields
            # The schedule's extra_data should provide these
            survey_spec = template.get("survey_spec")
            if survey_spec and isinstance(survey_spec, dict):
                survey_questions = survey_spec.get("spec", [])
                required_vars = [
                    q["variable"]
                    for q in survey_questions
                    if q.get("required", False)
                ]

                if required_vars:
                    provided_vars = set(extra_data.keys())
                    missing_vars = [v for v in required_vars if v not in provided_vars]

                    if missing_vars:
                        missing_required_vars.append(
                            {
                                "type": "schedules",
                                "id": schedule.get("id"),
                                "name": schedule.get("name"),
                                "url": schedule.get("url"),
                                "issue": "Missing required survey variables",
                                "missing_vars": missing_vars,
                                "template_id": ujt_id,
                                "template_name": template.get("name"),
                            }
                        )
                        logger.debug(
                            "schedule_missing_required_vars",
                            schedule_id=schedule.get("id"),
                            schedule_name=schedule.get("name"),
                            missing_vars=missing_vars,
                        )

        # Calculate totals
        total_issues = (
            len(extra_data_not_allowed)
            + len(inventory_not_allowed)
            + len(credential_variables)
            + len(missing_required_vars)
        )

        # Build result
        if total_issues > 0:
            details = {}
            if extra_data_not_allowed:
                details["extra_data_not_allowed"] = {
                    "count": len(extra_data_not_allowed),
                    "description": "Schedules with extra_data but template doesn't allow variables on launch",
                    "error_on_migration": "Variables X are not allowed on launch. Check the Prompt on Launch setting.",
                }
            if inventory_not_allowed:
                details["inventory_not_allowed"] = {
                    "count": len(inventory_not_allowed),
                    "description": "Schedules with inventory but template doesn't allow inventory on launch",
                    "error_on_migration": "Field is not configured to prompt on launch.",
                }
            if credential_variables:
                details["credential_variables"] = {
                    "count": len(credential_variables),
                    "description": "Schedules with credential-like variables (may fail credential injection)",
                    "error_on_migration": "Provided variable X has no database value to replace with.",
                }
            if missing_required_vars:
                details["missing_required_vars"] = {
                    "count": len(missing_required_vars),
                    "description": "Schedules missing required survey variables",
                    "error_on_migration": "'variable_name' value missing",
                }

            message = f"Found {total_issues} schedule configuration issues across {len(schedules)} schedules"

            recommendation = self._build_recommendation(
                len(extra_data_not_allowed),
                len(inventory_not_allowed),
                len(credential_variables),
                len(missing_required_vars),
            )

            # Credential variables are warnings, others are critical
            severity = Severity.CRITICAL
            if total_issues == len(credential_variables):
                # Only credential variable warnings
                severity = Severity.WARNING

            affected_resources = (
                extra_data_not_allowed
                + inventory_not_allowed
                + credential_variables
                + missing_required_vars
            )

            return HealthCheckResult(
                check_name=self.check_name,
                severity=severity,
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
                message=f"All {len(schedules)} schedules have valid configuration",
                details={},
                recommendation="",
                affected_resources=[],
                count=0,
            )

    def _build_recommendation(
        self,
        extra_data_count: int,
        inventory_count: int,
        credential_var_count: int,
        missing_vars_count: int,
    ) -> str:
        """Build remediation recommendation based on issues found.

        Args:
            extra_data_count: Number with extra_data not allowed
            inventory_count: Number with inventory not allowed
            credential_var_count: Number with credential variables
            missing_vars_count: Number missing required variables

        Returns:
            Formatted recommendation text
        """
        recommendations = []

        if extra_data_count > 0:
            recommendations.append(
                f"\n**Extra Data Not Allowed ({extra_data_count} schedules):**\n"
                "1. In source AAP UI, for each affected schedule:\n"
                "   Option A: Remove extra variables from schedule\n"
                "   Option B: Enable 'Prompt on Launch' for Variables in the job template\n"
                "2. To enable prompt on launch:\n"
                "   - Navigate to the job template\n"
                "   - Check 'Prompt on Launch' for 'Variables' field\n"
                "   - Save template\n"
                "\n"
                "**Why this fails:** AAP 2.6 validates that schedules can only provide "
                "extra_data if the job template allows variables on launch."
            )

        if inventory_count > 0:
            recommendations.append(
                f"\n**Inventory Not Allowed ({inventory_count} schedules):**\n"
                "1. In source AAP UI, for each affected schedule:\n"
                "   Option A: Remove inventory override from schedule\n"
                "   Option B: Enable 'Prompt on Launch' for Inventory in the job template\n"
                "2. To enable prompt on launch:\n"
                "   - Navigate to the job template\n"
                "   - Check 'Prompt on Launch' for 'Inventory' field\n"
                "   - Save template\n"
                "\n"
                "**Why this fails:** AAP 2.6 validates that schedules can only override "
                "inventory if the job template allows inventory on launch."
            )

        if credential_var_count > 0:
            recommendations.append(
                f"\n**Credential Variables ({credential_var_count} schedules - WARNING):**\n"
                "1. These schedules use credential injection variables (e.g., dbpwd, password, token)\n"
                "2. Verify that:\n"
                "   - The job template has the credential attached\n"
                "   - The credential has the required fields populated\n"
                "   - The variable name matches the credential field name\n"
                "3. Common failures:\n"
                "   - 'dbpwd has no database value to replace with' - credential missing database password\n"
                "   - Credential was deleted but schedule still references it\n"
                "\n"
                "**Why this might fail:** AAP 2.6 validates credential injection variables "
                "at schedule creation time. If the credential is missing or doesn't have the "
                "required field, migration will fail."
            )

        if missing_vars_count > 0:
            recommendations.append(
                f"\n**Missing Required Variables ({missing_vars_count} schedules):**\n"
                "1. In source AAP UI, navigate to each affected schedule\n"
                "2. Add the missing required variables to the schedule's 'Extra Variables' field\n"
                "3. The required variables are listed in the affected_resources details\n"
                "4. Alternatively, check the job template's Survey to see what values are expected\n"
                "\n"
                "**Why this fails:** AAP 2.6 validates that schedules provide all required "
                "survey variables. Job templates with surveys that have required fields will "
                "reject schedules that don't provide those variables."
            )

        if recommendations:
            header = (
                "Fix the following schedule issues in source AAP before migration:\n"
            )
            return header + "\n".join(recommendations)
        else:
            return ""
