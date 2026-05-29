"""Playbook validation health check.

Detects job templates referencing playbooks that don't exist in their project's
playbook cache. This is the #1 cause of job template migration failures:

- 800 failures (74% of all job template failures) in real customer data
- Error: "playbook: ['Playbook not found for project.']"
- Root cause: Playbook renamed/deleted in Git, but AAP 2.4 retains cached reference

AAP 2.6 validates that playbooks exist at import time, so stale references
that silently work in AAP 2.4 will fail during migration.
"""

import asyncio
from typing import Any

import structlog

from aap_migration.health.base_check import BaseHealthCheck
from aap_migration.health.models import CheckStatus, HealthCheckResult, Severity

logger = structlog.get_logger()


class PlaybookValidationCheck(BaseHealthCheck):
    """Validate that job template playbook references exist in their projects.

    Cross-references each job template's playbook field against the project's
    cached playbook list (GET /api/v2/projects/{id}/playbooks/).

    Based on real customer data analysis:
    - 800 failures: Playbook not found in project
    - 74% of all job template migration failures
    """

    # Concurrency limit for project playbook fetches.
    # Reduced from 10 to 3 to avoid overwhelming the API at scale
    # (8,000+ individual per-project calls in large environments).
    MAX_CONCURRENT_FETCHES = 3

    # Delay between individual project playbook fetches (seconds).
    # Prevents burst traffic that triggers rate limiting.
    FETCH_DELAY = 0.2

    @property
    def check_name(self) -> str:
        """Name of the health check."""
        return "playbook_validation"

    @property
    def description(self) -> str:
        """Description of what this check validates."""
        return "Validate job template playbook references exist in projects"

    async def run(self) -> HealthCheckResult:
        """Execute the playbook validation check.

        Steps:
        1. Fetch all job templates
        2. Extract unique project IDs
        3. Fetch playbooks for each project (with concurrency control)
        4. Cross-reference job templates against project playbooks
        5. Return result

        Returns:
            HealthCheckResult with findings
        """
        logger.info(
            "health_check_starting",
            check=self.check_name,
            description=self.description,
        )

        # Step 1: Fetch all job templates
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

        # Step 2: Extract unique project IDs (skip templates without a project)
        unique_project_ids = {
            jt["project"] for jt in job_templates if jt.get("project") is not None
        }

        logger.info(
            "unique_projects_identified",
            count=len(unique_project_ids),
        )

        # Step 3: Fetch playbooks for each unique project
        project_playbooks, failed_projects = await self._fetch_all_project_playbooks(
            unique_project_ids
        )

        # Step 4: Cross-reference job templates against project playbooks
        playbook_not_found = []
        cannot_verify = []

        for jt in job_templates:
            project_id = jt.get("project")
            playbook = jt.get("playbook")

            # Skip templates without a project (caught by job_template_validation)
            if project_id is None:
                continue

            # Skip templates without a playbook field
            if not playbook:
                continue

            # Check if we failed to fetch this project's playbooks
            if project_id in failed_projects:
                cannot_verify.append(
                    {
                        "type": "job_templates",
                        "id": jt.get("id"),
                        "name": jt.get("name"),
                        "url": jt.get("url"),
                        "issue": f"Cannot verify playbook - failed to fetch playbook list for project {project_id}",
                        "project_id": project_id,
                        "playbook": playbook,
                    }
                )
                continue

            # Get the project's playbook list
            available_playbooks = project_playbooks.get(project_id, [])

            # Check if the playbook exists in the project
            if playbook not in available_playbooks:
                # Look up project name from job template's summary_fields if available
                summary_fields = jt.get("summary_fields", {})
                project_info = summary_fields.get("project", {})
                project_name = project_info.get("name", f"Project {project_id}")

                playbook_not_found.append(
                    {
                        "type": "job_templates",
                        "id": jt.get("id"),
                        "name": jt.get("name"),
                        "url": jt.get("url"),
                        "issue": f"Playbook '{playbook}' not found in project",
                        "project_id": project_id,
                        "project_name": project_name,
                        "playbook": playbook,
                        "available_playbooks": available_playbooks,
                    }
                )
                logger.debug(
                    "playbook_not_found_in_project",
                    job_template_id=jt.get("id"),
                    job_template_name=jt.get("name"),
                    playbook=playbook,
                    project_id=project_id,
                    available_playbooks=available_playbooks,
                )

        # Step 5: Build result
        total_issues = len(playbook_not_found) + len(cannot_verify)

        if total_issues > 0:
            details = {}
            if playbook_not_found:
                details["playbook_not_found"] = {
                    "count": len(playbook_not_found),
                    "description": "Job templates with playbooks that don't exist in project",
                    "error_on_migration": "playbook: ['Playbook not found for project.']",
                }
            if cannot_verify:
                details["cannot_verify"] = {
                    "count": len(cannot_verify),
                    "description": "Job templates whose project playbooks could not be fetched",
                }

            message = (
                f"Found {len(playbook_not_found)} job templates with invalid playbook "
                f"references across {len(job_templates)} templates"
            )
            if cannot_verify:
                message += f" ({len(cannot_verify)} could not be verified)"

            recommendation = self._build_recommendation(
                len(playbook_not_found),
                len(cannot_verify),
            )

            affected_resources = playbook_not_found + cannot_verify

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
                message=(
                    f"All {len(job_templates)} job templates have valid "
                    f"playbook references ({len(unique_project_ids)} projects checked)"
                ),
                details={},
                recommendation="",
                affected_resources=[],
                count=0,
            )

    async def _fetch_all_project_playbooks(
        self,
        project_ids: set[int],
    ) -> tuple[dict[int, list[str]], set[int]]:
        """Fetch playbook lists for all projects with concurrency control.

        Uses a semaphore to limit concurrent requests and adds a small delay
        between fetches to avoid overwhelming the API. Progress is logged
        periodically for long-running operations.

        Args:
            project_ids: Set of project IDs to fetch playbooks for

        Returns:
            Tuple of:
            - Dict mapping project_id -> list of playbook filenames
            - Set of project IDs that failed to fetch
        """
        semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_FETCHES)
        project_playbooks: dict[int, list[str]] = {}
        failed_projects: set[int] = set()
        completed_count = 0
        total = len(project_ids)

        logger.info(
            "fetching_project_playbooks",
            total_projects=total,
            concurrency=self.MAX_CONCURRENT_FETCHES,
        )

        async def fetch_one(project_id: int) -> None:
            nonlocal completed_count
            async with semaphore:
                try:
                    playbooks = await self._fetch_project_playbooks(project_id)
                    project_playbooks[project_id] = playbooks
                except Exception as e:
                    logger.warning(
                        "failed_to_fetch_project_playbooks",
                        project_id=project_id,
                        error=str(e),
                    )
                    failed_projects.add(project_id)
                finally:
                    completed_count += 1
                    # Log progress every 100 projects for visibility
                    if completed_count % 100 == 0 or completed_count == total:
                        logger.info(
                            "playbook_fetch_progress",
                            completed=completed_count,
                            total=total,
                            failed_so_far=len(failed_projects),
                        )

                # Small delay to avoid API burst traffic
                await asyncio.sleep(self.FETCH_DELAY)

        # Launch all fetches with semaphore-controlled concurrency
        tasks = [fetch_one(pid) for pid in project_ids]
        await asyncio.gather(*tasks)

        logger.info(
            "project_playbooks_fetched",
            total_projects=total,
            successful=len(project_playbooks),
            failed=len(failed_projects),
        )

        return project_playbooks, failed_projects

    async def _fetch_project_playbooks(self, project_id: int) -> list[str]:
        """Fetch the playbook list for a single project.

        Calls GET /api/v2/projects/{id}/playbooks/ which returns a JSON array
        of playbook filenames (e.g., ["site.yml", "main.yml"]).

        Args:
            project_id: Project ID

        Returns:
            List of playbook filenames
        """
        response = await self.client.get(f"projects/{project_id}/playbooks/")

        # The playbooks endpoint returns a JSON array directly, but the client
        # may wrap it or return it as-is depending on the response format.
        if isinstance(response, list):
            return response
        elif isinstance(response, dict):
            # Some API versions may return {"results": [...]} or similar
            return response.get("results", response.get("playbooks", []))
        else:
            logger.warning(
                "unexpected_playbooks_response_type",
                project_id=project_id,
                response_type=type(response).__name__,
            )
            return []

    def _build_recommendation(
        self,
        playbook_not_found_count: int,
        cannot_verify_count: int,
    ) -> str:
        """Build remediation recommendation based on issues found.

        Args:
            playbook_not_found_count: Number of templates with missing playbooks
            cannot_verify_count: Number of templates that could not be verified

        Returns:
            Formatted recommendation text
        """
        recommendations = []

        if playbook_not_found_count > 0:
            recommendations.append(
                f"\n**Playbook Not Found ({playbook_not_found_count} templates):**\n"
                "These job templates reference playbooks that don't exist in their "
                "project's playbook cache.\n"
                "\n"
                "Possible causes:\n"
                "1. Playbook was renamed or deleted in the Git repository\n"
                "2. Project never synced successfully\n"
                "3. Wrong playbook path specified\n"
                "\n"
                "Action required:\n"
                "1. In source AAP UI, navigate to each affected job template\n"
                "2. Check the project's playbook list (Project > Playbooks tab)\n"
                "3. Either:\n"
                "   - Update the job template to use a valid playbook from the list, OR\n"
                "   - Fix the project's Git repository to include the missing playbook\n"
                "   - Re-sync the project to update the playbook cache\n"
                "\n"
                "**Why this fails:** AAP 2.6 validates that playbooks exist at import "
                "time. Templates referencing non-existent playbooks will fail to migrate."
            )

        if cannot_verify_count > 0:
            recommendations.append(
                f"\n**Cannot Verify ({cannot_verify_count} templates):**\n"
                "The playbook list could not be fetched for these templates' projects. "
                "This may indicate the project has been deleted or is inaccessible.\n"
                "\n"
                "Action required:\n"
                "1. Check if the project still exists in source AAP\n"
                "2. If the project exists, verify API permissions allow reading playbooks\n"
                "3. If the project is deleted, assign a valid project to the job template"
            )

        if recommendations:
            header = (
                "Fix the following job templates before migration:\n"
            )
            return header + "\n".join(recommendations)
        else:
            return ""
