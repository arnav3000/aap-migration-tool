"""
Project failure analysis and reporting command.

This module provides utilities to analyze why projects failed to import
and generate detailed manual intervention instructions.
"""

import json
from pathlib import Path

import click

from aap_migration.cli.context import MigrationContext
from aap_migration.cli.decorators import handle_errors, pass_context, requires_config
from aap_migration.cli.utils import echo_error, echo_info, echo_success, echo_warning
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


@click.command(name="analyze-project-failures", hidden=True)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default="PROJECT-FAILURES-REPORT.md",
    help="Output file for failure report (default: PROJECT-FAILURES-REPORT.md)",
)
@pass_context
@requires_config
@handle_errors
def analyze_project_failures(ctx: MigrationContext, output: Path) -> None:
    """Analyze failed project imports and generate manual fix instructions.

    This command checks which projects failed to import, identifies the root cause,
    and provides step-by-step instructions for manual intervention.
    """
    # Load transformed projects
    projects_dir = Path(ctx.config.paths.transform_dir) / "projects"
    if not projects_dir.exists():
        echo_error("No transformed projects directory found!")
        return

    json_files = sorted(projects_dir.glob("projects_*.json"))
    if not json_files:
        echo_error("No project files found!")
        return

    all_projects = []
    for json_file in json_files:
        with open(json_file) as f:
            all_projects.extend(json.load(f))

    echo_info(f"Analyzing {len(all_projects)} projects...")

    # Categorize projects
    imported_projects = []
    failed_to_import = []
    patch_failed = []
    fully_successful = []

    for project in all_projects:
        source_id = project.get("_source_id", project.get("id"))
        name = project.get("name")
        has_deferred = "_deferred_scm_details" in project

        # Check if imported
        target_id = ctx.migration_state.get_mapped_id("projects", source_id)

        if not target_id:
            # Failed to import at all
            failed_to_import.append(
                {
                    "source_id": source_id,
                    "name": name,
                    "organization": project.get("organization"),
                    "has_scm": has_deferred,
                    "deferred_details": project.get("_deferred_scm_details", {}),
                }
            )
        else:
            imported_projects.append(
                {
                    "source_id": source_id,
                    "target_id": target_id,
                    "name": name,
                    "has_scm": has_deferred,
                }
            )

            if has_deferred:
                # TODO: Check if patch was successful by querying target
                # For now, assume if imported + deferred, it needs patching
                patch_failed.append(
                    {
                        "source_id": source_id,
                        "target_id": target_id,
                        "name": name,
                    }
                )
            else:
                fully_successful.append(
                    {
                        "source_id": source_id,
                        "target_id": target_id,
                        "name": name,
                    }
                )

    # Generate report
    report_lines = []
    report_lines.append("# Project Migration Failure Analysis\n")
    report_lines.append(f"**Generated:** {ctx.config.paths.transform_dir}\n")
    report_lines.append(f"**Total Projects:** {len(all_projects)}\n\n")

    report_lines.append("## Summary\n\n")
    report_lines.append(f"- ✅ **Fully Successful:** {len(fully_successful)} projects\n")
    report_lines.append(
        f"- ✅ **Imported (needs patching):** {len(imported_projects) - len(fully_successful)} projects\n"
    )
    report_lines.append(f"- ❌ **Failed to Import:** {len(failed_to_import)} projects\n\n")

    if failed_to_import:
        report_lines.append("---\n\n")
        report_lines.append("## ❌ Projects That Failed to Import\n\n")
        report_lines.append("These projects never imported and have no target_id mapping.\n\n")

        for idx, proj in enumerate(failed_to_import, 1):
            report_lines.append(f"### {idx}. {proj['name']} (Source ID: {proj['source_id']})\n\n")

            report_lines.append("**Likely Causes:**\n")
            report_lines.append("- Name collision with existing project in target AAP\n")
            report_lines.append("- Organization dependency not resolved\n")
            report_lines.append("- SCM credential mapping failed\n\n")

            report_lines.append("**Manual Fix Steps:**\n\n")
            report_lines.append("1. **Check for name collision:**\n")
            report_lines.append("   ```bash\n")
            report_lines.append(f"   # Check if '{proj['name']}' exists in target AAP\n")
            report_lines.append("   curl -sk -H 'Authorization: Bearer $TOKEN' \\\n")
            # Extract base URL: strip path-based /api suffixes but preserve hostname
            from urllib.parse import quote, urlparse, urlunparse

            parsed = urlparse(ctx.config.target.url)
            path = parsed.path
            api_idx = path.find("/api")
            if api_idx >= 0:
                path = path[:api_idx]
            base_url = urlunparse((parsed.scheme, parsed.netloc, path.rstrip("/"), "", "", ""))
            encoded_name = quote(proj["name"], safe="")
            report_lines.append(f"     '{base_url}/api/v2/projects/?name={encoded_name}'\n")
            report_lines.append("   ```\n\n")

            report_lines.append("2. **If name collision exists:**\n")
            report_lines.append("   - Rename the existing project in target AAP, OR\n")
            report_lines.append("   - Manually map this project to the existing one:\n")
            report_lines.append("     ```sql\n")
            report_lines.append("     -- In migration_state.db\n")
            report_lines.append(
                "     INSERT INTO id_mappings (resource_type, source_id, target_id, source_name, target_name)\n"
            )
            safe_name = proj["name"].replace("'", "''")
            report_lines.append(
                f"     VALUES ('projects', {proj['source_id']}, <EXISTING_TARGET_ID>, '{safe_name}', '{safe_name}');\n"
            )
            report_lines.append("     ```\n\n")

            report_lines.append("3. **If no collision, create manually:**\n")

            if proj["has_scm"]:
                deferred = proj["deferred_details"]
                scm_url = deferred.get("scm_url", "N/A")
                scm_type = deferred.get("scm_type", "git")
                scm_branch = deferred.get("scm_branch", "")
                cred_id = deferred.get("credential")

                import json as json_mod

                json_name = json_mod.dumps(proj["name"])[1:-1]
                json_scm_url = json_mod.dumps(scm_url)[1:-1]
                json_scm_type = json_mod.dumps(scm_type)[1:-1]

                report_lines.append("   ```bash\n")
                report_lines.append("   # Create project via API\n")
                report_lines.append("   curl -sk -X POST -H 'Authorization: Bearer $TOKEN' \\\n")
                report_lines.append("     -H 'Content-Type: application/json' \\\n")
                report_lines.append(f"     '{base_url}/api/v2/projects/' \\\n")
                report_lines.append("     -d '{\n")
                report_lines.append(f'       "name": "{json_name}",\n')
                report_lines.append('       "organization": <TARGET_ORG_ID>,\n')
                report_lines.append(f'       "scm_type": "{json_scm_type}",\n')
                report_lines.append(f'       "scm_url": "{json_scm_url}",\n')
                if scm_branch:
                    json_scm_branch = json_mod.dumps(scm_branch)[1:-1]
                    report_lines.append(f'       "scm_branch": "{json_scm_branch}",\n')
                if cred_id:
                    report_lines.append('       "credential": <TARGET_CREDENTIAL_ID>,\n')
                report_lines.append("     }'\n")
                report_lines.append("   ```\n\n")
            else:
                report_lines.append("   - Create as Manual project in AAP UI\n")
                report_lines.append(f"   - Name: `{proj['name']}`\n")
                report_lines.append(
                    f"   - Organization: (check mapping for source org {proj['organization']})\n\n"
                )

            report_lines.append("4. **After manual creation, update mapping:**\n")
            report_lines.append("   ```sql\n")
            report_lines.append(
                "   INSERT INTO id_mappings (resource_type, source_id, target_id, source_name, target_name)\n"
            )
            report_lines.append(
                f"   VALUES ('projects', {proj['source_id']}, <NEW_TARGET_ID>, '{safe_name}', '{safe_name}');\n"
            )
            report_lines.append("   ```\n\n")

            report_lines.append("---\n\n")

    if imported_projects:
        needs_patch = [p for p in imported_projects if p["has_scm"]]
        if needs_patch:
            report_lines.append("## ⚠️  Imported Projects That Need SCM Patching\n\n")
            report_lines.append(
                f"These {len(needs_patch)} projects imported as 'Manual' and need SCM details applied.\n\n"
            )
            report_lines.append("**To fix, run:**\n")
            report_lines.append("```bash\n")
            report_lines.append("aap-bridge patch-projects\n")
            report_lines.append("```\n\n")

    report_lines.append("## 🔄 Resume Migration After Fixes\n\n")
    report_lines.append("Once you've manually fixed the failed projects above:\n\n")
    report_lines.append("1. **Re-run Phase 3 import** to import job templates:\n")
    report_lines.append("   ```bash\n")
    report_lines.append("   aap-bridge import --yes --phase phase3 \\\n")
    report_lines.append("     --resource-type job_templates \\\n")
    report_lines.append("     --resource-type workflow_job_templates \\\n")
    report_lines.append("     --resource-type schedules \\\n")
    report_lines.append("     --resource-type notification_templates\n")
    report_lines.append("   ```\n\n")

    report_lines.append("2. **Verify completion:**\n")
    report_lines.append("   ```bash\n")
    report_lines.append(
        '   sqlite3 migration_state.db "SELECT resource_type, COUNT(*) as total, \\\n'
    )
    report_lines.append(
        "     SUM(CASE WHEN target_id IS NOT NULL THEN 1 ELSE 0 END) as imported \\\n"
    )
    report_lines.append('     FROM id_mappings GROUP BY resource_type;"\n')
    report_lines.append("   ```\n\n")

    # Write report
    with open(output, "w") as f:
        f.writelines(report_lines)

    # Print summary
    echo_success(f"Analysis complete! Report saved to: {output}")
    echo_info("\n📊 Summary:")
    echo_info(f"  ✅ Fully Successful: {len(fully_successful)}")
    echo_info(f"  ⚠️  Imported (needs patching): {len(imported_projects) - len(fully_successful)}")
    echo_info(f"  ❌ Failed to Import: {len(failed_to_import)}")

    if failed_to_import:
        echo_warning(f"\n⚠️  {len(failed_to_import)} projects require manual intervention!")
        echo_warning(f"   See {output} for detailed fix instructions.")
