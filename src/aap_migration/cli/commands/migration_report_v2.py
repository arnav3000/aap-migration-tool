"""
Enhanced migration report command (V2).

This module provides the 'enhanced-report' CLI command that generates an
interactive report with:
- Dynamic error classification at runtime
- Individual error selector populated from actual error messages
- Resource metadata enrichment (Last Modified, Modified By, Resource Status in AAP,
  Last Job Run, Sync Status, Created, Next Run)
- Multi-format output: HTML (interactive), Markdown, CSV
- Organization-level and cross-organization views
- Resizable and sortable table columns
- Click-to-expand error detail modal
- Group errors by pattern toggle
- CSV export from browser (HTML mode)
- Pending status tracking

Usage:
    aap-bridge enhanced-report
    aap-bridge enhanced-report --output /tmp/report.html
    aap-bridge enhanced-report --resource-type credentials
    aap-bridge enhanced-report --format markdown
    aap-bridge enhanced-report --format csv
"""

import csv as csv_module
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import click

from aap_migration.cli.context import MigrationContext
from aap_migration.cli.decorators import handle_errors, pass_context, requires_config
from aap_migration.cli.utils import echo_error, echo_info, echo_success
from aap_migration.migration.database import get_session
from aap_migration.migration.models import MigrationProgress
from aap_migration.reporting.org_mapper import OrganizationMapper
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Dynamic error normalization (runtime extraction, no hardcoded categories)
# ---------------------------------------------------------------------------
def normalize_error_key(error_message: str) -> str:
    """Extract a short, human-readable error key from a full error message.

    Instead of matching against predefined regex categories, this function
    dynamically normalizes the error message into a short label by detecting
    its structure at runtime.
    """
    if not error_message:
        return ""

    msg = error_message.strip()

    if msg.startswith("Skipped: Pre-existing"):
        return "Pre-existing in target"

    if "Duplicate exists in target" in msg:
        return "Duplicate exists in target"

    if msg.startswith("Skipped: Duplicate"):
        match = re.match(r"Skipped: Duplicate (\w+)", msg)
        resource = match.group(1) if match else "resource"
        return f"Duplicate {resource}"

    if msg.startswith("Validation failed:"):
        detail = msg.replace("Validation failed: ", "").split(".")[0].strip()
        detail = re.sub(r"'[^']*'", "'...'", detail)
        detail = re.sub(r"\(source ID \d+\)", "", detail).strip()
        return f"Validation: {detail}"

    if msg.startswith("APIError:") or msg.startswith("API error:"):
        cleaned = re.sub(r"^(APIError:|API error:)\s*", "", msg)
        cleaned = re.sub(r"\[400\]\s*", "", cleaned)

        if "playbook" in cleaned.lower() and "not found" in cleaned.lower():
            return "API: Playbook not found for project"
        if "must have a project assigned" in cleaned.lower():
            return "API: Job Template must have a project"
        if "must have a credential assigned" in cleaned.lower():
            return "API: Job Template must have a credential"
        if "variables_needed_to_start" in cleaned:
            vars_match = re.findall(r"'([^']+)' value missing", cleaned)
            if vars_match:
                return f"API: variables_needed_to_start ({', '.join(vars_match[:3])}{'...' if len(vars_match) > 3 else ''})"
            return "API: variables_needed_to_start"
        if "unified_job_template" in cleaned and "does not exist" in cleaned:
            return "API: unified_job_template not found in target"
        if "extra_data" in cleaned and "not allowed" in cleaned.lower():
            return "API: extra_data not allowed on launch"
        if "source_path" in cleaned:
            return "API: Cannot set source_path if not SCM type"
        if "Smart or Constructed" in cleaned:
            return "API: Cannot create source for Smart/Constructed Inventory"
        if "not a valid hostname" in cleaned:
            return "API: Invalid hostname in policy_instance_list"
        if "provisioning callback" in cleaned:
            return "API: Cannot enable provisioning callback without inventory"
        if "ssh_key_unlock" in cleaned:
            return "API: ssh_key_unlock set when key not encrypted"
        if "This field is required" in cleaned:
            field_match = re.search(r"(\w+):\s*[\['\"]", cleaned)
            field_name = field_match.group(1) if field_match else "unknown"
            return f"API: Field required ({field_name})"
        if "Credential is required" in cleaned:
            return "API: Credential required for cloud source"
        if "not configured to prompt on launch" in cleaned.lower():
            return "API: Field not configured to prompt on launch"
        if "variables are not allowed on launch" in cleaned.lower():
            return "API: Variables not allowed on launch"
        if "maximum number of" in cleaned.lower():
            return "API: Host subscription limit reached"
        if "inventory" in cleaned.lower() and ("null" in cleaned.lower() or "required" in cleaned.lower()):
            return "API: Inventory required"

        short = cleaned[:80].split(".")[0].split("'")[0].strip()
        short = re.sub(r"\s+", " ", short)
        return f"API: {short}" if short else "API: Unknown error"

    if msg.startswith("WARNING:"):
        detail = msg.replace("WARNING: ", "").split(".")[0].strip()[:60]
        return f"Warning: {detail}"

    if msg.startswith("Cannot import workflow"):
        return "Cannot import workflow: template(s) failed to import"

    short = msg[:80].split(".")[0].strip()
    return short if short else "Unknown error"


# ---------------------------------------------------------------------------
# Export metadata enrichment
# ---------------------------------------------------------------------------
_EXPORT_META_FIELDS = {
    "created", "modified", "last_job_run", "last_job_failed",
    "next_job_run", "last_update_failed",
}


def _format_user_info(user_info: dict | None) -> str:
    """Format a user info dict (created_by / modified_by) into a readable string."""
    if not user_info or not isinstance(user_info, dict):
        return "N/A"
    username = user_info.get("username", "")
    first = user_info.get("first_name", "")
    last = user_info.get("last_name", "")
    if first or last:
        return f"{username} ({first} {last})".strip()
    return username or "N/A"


_HUMAN_READABLE_ERRORS: dict[str, str] = {
    "API: Playbook not found for project": (
        "Playbook '{playbook}' was not found in project '{project_name}'."
    ),
    "API: Job Template must have a project": (
        "Job template '{source_name}' has no project assigned."
    ),
    "API: Job Template must have a credential": (
        "Job template '{source_name}' has no credential assigned."
    ),
    "API: unified_job_template not found in target": (
        "The referenced template or workflow (source ID in error)"
        " was not imported to the target AAP."
    ),
    "API: Inventory required": (
        "Job template '{source_name}' requires an inventory but none was"
        " assigned (expected inventory: '{inventory_name}')."
    ),
    "API: variables_needed_to_start": (
        "The schedule for '{source_name}' requires survey variables that"
        " are not present in the schedule's extra_data. In AAP 2.4,"
        " schedules only needed to include variables they wanted to"
        " override — any missing required variable would use the survey's"
        " default value at runtime. In AAP 2.6, schedules must include ALL"
        " required survey variables in extra_data at creation time, even if"
        " they match the survey defaults. This failure is caused by AAP 2.6's"
        " stricter validation, not missing data — the migration tool preserves"
        " schedule data exactly as-is from AAP 2.4."
    ),
    "API: extra_data not allowed on launch": (
        "The schedule for '{source_name}' provides extra_data but the parent"
        " template does not allow it (ask_variables_on_launch=false)."
    ),
    "API: Field not configured to prompt on launch": (
        "The schedule for '{source_name}' overrides a field that the parent"
        " template does not allow via 'Prompt on Launch' settings."
    ),
    "API: Variables not allowed on launch": (
        "The schedule for '{source_name}' passes variables but the parent"
        " template does not allow variables on launch."
    ),
    "API: Cannot set source_path if not SCM type": (
        "Inventory source has source_path set but the source type is not"
        " SCM-based."
    ),
    "API: Cannot create source for Smart/Constructed Inventory": (
        "Cannot create an inventory source for a Smart or Constructed"
        " inventory."
    ),
    "API: Invalid hostname in policy_instance_list": (
        "The policy_instance_list contains a hostname that does not exist"
        " in the target AAP cluster."
    ),
    "API: Cannot enable provisioning callback without inventory": (
        "Job template has provisioning callback enabled but no inventory"
        " assigned."
    ),
    "API: ssh_key_unlock set when key not encrypted": (
        "Credential has ssh_key_unlock set but the SSH key is not encrypted."
    ),
    "API: Host subscription limit reached": (
        "The target AAP organization has reached its maximum host"
        " subscription limit."
    ),
    "API: Credential required for cloud source": (
        "Inventory source requires a cloud credential but none was assigned."
    ),
    "Duplicate exists in target": (
        "{resource_type} '{source_name}' (source ID: {source_id})"
        " already exists in the target AAP (skipped to avoid duplicates)."
    ),
    "Pre-existing in target": (
        "{resource_type} '{source_name}' (source ID: {source_id})"
        " was already present in the target AAP before migration."
    ),
}


def _build_export_lookup(export_dir: Path) -> dict:
    """Build lookup table from export files: (resource_type, source_id) -> metadata.

    Extracts: modified, modified_by, created, last_job_run, last_job_failed,
    next_job_run, sync_status (from export 'status' field).
    """
    lookup: dict = {}
    if not export_dir.is_dir():
        logger.warning(f"Exports directory not found: {export_dir}")
        return lookup

    for resource_dir in export_dir.iterdir():
        if not resource_dir.is_dir():
            continue

        resource_type = resource_dir.name
        json_files = sorted(resource_dir.glob("*.json"))

        for json_file in json_files:
            try:
                with open(json_file, "r") as f:
                    items = json.load(f)
            except (json.JSONDecodeError, IOError):
                continue

            if not isinstance(items, list):
                continue

            for item in items:
                sid = item.get("_source_id") or item.get("id")
                if sid is None:
                    continue

                modified = item.get("modified", "")
                summary_fields = item.get("summary_fields", {})

                created_by_info = summary_fields.get("created_by")
                created_by = _format_user_info(created_by_info)

                modified_by_info = summary_fields.get("modified_by")
                modified_by = _format_user_info(modified_by_info)

                meta = {
                    "modified": modified,
                    "created_by": created_by,
                    "modified_by": modified_by,
                }

                for field in _EXPORT_META_FIELDS:
                    val = item.get(field)
                    if val is not None:
                        meta[field] = val

                if item.get("status") is not None:
                    meta["sync_status"] = item["status"]

                playbook = item.get("playbook")
                if playbook:
                    meta["playbook"] = playbook

                proj_info = summary_fields.get("project")
                if isinstance(proj_info, dict) and proj_info.get("name"):
                    meta["project_name"] = proj_info["name"]

                inv_info = summary_fields.get("inventory")
                if isinstance(inv_info, dict) and inv_info.get("name"):
                    meta["inventory_name"] = inv_info["name"]

                creds_info = summary_fields.get("credentials")
                if isinstance(creds_info, list) and creds_info:
                    meta["credential_names"] = ", ".join(
                        c["name"] for c in creds_info if isinstance(c, dict) and c.get("name")
                    )

                org_info = summary_fields.get("organization")
                if isinstance(org_info, dict) and org_info.get("name"):
                    meta["organization_name"] = org_info["name"]

                for scm_field in ("scm_type", "scm_url", "scm_branch"):
                    scm_val = item.get(scm_field)
                    if scm_val:
                        meta[scm_field] = scm_val

                lookup[(resource_type, sid)] = meta

    return lookup


def _build_user_email_lookup(export_dir: Path, org_mapper: OrganizationMapper, source_config=None) -> dict[str, list[str]]:
    """Build org_name -> [emails] mapping from export user data, excluding auditors.

    Strategy:
    1. Read organization export to get org IDs and names.
    2. Fetch /api/v2/organizations/{id}/users/ from source API to build
       user_id → [org_ids] mapping (users are global in AAP, membership is via roles).
    3. Read user export for emails and is_system_auditor.
    4. Match user_id → org_names using the API-fetched membership.
    5. Exclude system auditors and users without email.
    6. A user belonging to multiple orgs will appear in ALL of them.

    Falls back to org_mapper if the source API is unreachable.
    """
    users_dir = export_dir / "users"
    if not users_dir.is_dir():
        return {}

    # Load users from export: _source_id → {email, is_system_auditor}
    user_data: dict[int, dict] = {}
    for json_file in sorted(users_dir.glob("*.json")):
        try:
            with open(json_file, "r") as f:
                items = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue
        if not isinstance(items, list):
            continue
        for user in items:
            uid = user.get("_source_id") or user.get("id")
            if uid is None:
                continue
            user_data[uid] = {
                "email": user.get("email", "").strip(),
                "is_system_auditor": user.get("is_system_auditor", False),
            }

    if not user_data:
        return {}

    # Build user_id → [org_names] mapping from source API
    user_orgs_map: dict[int, list[str]] = {}

    if source_config is not None:
        user_orgs_map = _fetch_org_user_memberships(export_dir, source_config)

    # If API fetch failed or wasn't attempted, fall back to org_mapper
    if not user_orgs_map:
        for uid in user_data:
            org_name = org_mapper.get_organization_name("users", uid)
            user_orgs_map[uid] = [org_name]

    # Assemble org_name → [emails], excluding auditors and empty emails.
    # A user in multiple orgs appears under each org.
    org_emails: dict[str, list[str]] = defaultdict(list)
    for uid, info in user_data.items():
        if info["is_system_auditor"]:
            continue
        if not info["email"]:
            continue
        org_names = user_orgs_map.get(uid, ["(Unknown)"])
        for org_name in org_names:
            org_emails[org_name].append(info["email"])

    return dict(org_emails)


def _fetch_org_user_memberships(export_dir: Path, source_config) -> dict[int, list[str]]:
    """Fetch user→org membership from source API for all exported organizations.

    Calls GET /api/v2/organizations/{id}/users/?page_size=200 for each org,
    using connection pooling and batched requests for performance on large datasets.

    Returns:
        Mapping of user_id → [org_names]. A user belonging to multiple
        organizations will have all org names in the list.
    """
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    import httpx

    # Read orgs from export to get (org_id, org_name) pairs
    orgs_dir = export_dir / "organizations"
    if not orgs_dir.is_dir():
        return {}

    org_list: list[tuple[int, str]] = []
    for json_file in sorted(orgs_dir.glob("*.json")):
        try:
            with open(json_file, "r") as f:
                items = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue
        if not isinstance(items, list):
            continue
        for org in items:
            org_id = org.get("id") or org.get("_source_id")
            org_name = org.get("name")
            if org_id and org_name:
                org_list.append((org_id, org_name))

    if not org_list:
        return {}

    org_list.sort(key=lambda x: x[1])

    # Build API session
    base_url = source_config.url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {source_config.token}",
        "Content-Type": "application/json",
    }
    verify = source_config.verify_ssl
    timeout = getattr(source_config, "timeout", 60)

    user_orgs_map: dict[int, list[str]] = defaultdict(list)

    try:
        with httpx.Client(
            headers=headers,
            verify=verify,
            timeout=timeout,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        ) as client:
            for org_id, org_name in org_list:
                try:
                    _fetch_org_members(
                        client, base_url, org_id, org_name, user_orgs_map
                    )
                except Exception as e:
                    logger.warning(
                        "org_users_fetch_failed",
                        org_id=org_id,
                        org_name=org_name,
                        error=str(e),
                    )
                    continue

    except Exception as e:
        logger.warning(
            "user_email_api_fetch_failed",
            error=str(e),
            message="Falling back to org_mapper for user-org mapping",
        )
        return {}

    logger.info(
        "user_org_memberships_fetched",
        total_users_mapped=len(user_orgs_map),
        organizations_queried=len(org_list),
    )
    return dict(user_orgs_map)


def _fetch_org_members(
    client, base_url: str, org_id: int, org_name: str, user_orgs_map: dict[int, list[str]]
) -> None:
    """Fetch all user IDs for a single organization with pagination.

    Queries both /users/ (member role) and /admins/ (admin role) endpoints
    to capture all users associated with the organization.
    """
    for endpoint in ("users", "admins"):
        page = 1
        page_size = 200

        while True:
            resp = client.get(
                f"{base_url}/organizations/{org_id}/{endpoint}/",
                params={"page": page, "page_size": page_size},
            )
            resp.raise_for_status()
            data = resp.json()

            for user in data.get("results", []):
                uid = user.get("id")
                if uid is not None and org_name not in user_orgs_map[uid]:
                    user_orgs_map[uid].append(org_name)

            if not data.get("next"):
                break
            page += 1


_MISSING_DEPENDENCY_PATTERNS = [
    "playbook not found",
    "must have a project assigned",
    "must have a credential assigned",
    "unified_job_template",
    "does not exist",
    "not found",
    "credential is required",
    "field required",
    "inventory required",
]

# Required fields per resource type (AAP 2.4 documentation).
# Each entry maps a resource type to a list of (metadata_key, human_label)
# tuples.  If any metadata_key is absent or empty the resource is considered
# "Probably Stale" because a mandatory linked object is missing.
_REQUIRED_FIELDS_BY_TYPE: dict[str, list[tuple[str, str]]] = {
    "job_templates": [
        ("project_name", "project"),
    ],
    "inventory_sources": [
        ("credential_names", "credential"),
    ],
    "workflow_job_template_nodes": [
        ("project_name", "unified_job_template"),
    ],
}


def _determine_resource_status(resource: dict) -> str:
    """Classify a resource's staleness.

    Rules:
    - **projects**: 'Probably Stale' when last modified > 1 year ago AND
      sync_status is in a failed state.
    - **all resources**: 'Probably Stale' when a required field for that
      resource type has no linked object assigned (e.g. a job_template with
      no project).  Required fields are defined in ``_REQUIRED_FIELDS_BY_TYPE``
      based on the AAP 2.4 API documentation.
    - **all resources**: 'Probably Stale' when the migration error message
      indicates a missing dependency (playbook not found, project missing,
      credential missing, etc.).
    - Otherwise 'Active' or 'Unknown' (if no modified date).
    """
    resource_type = resource.get("resource_type", "")
    modified_str = resource.get("modified", "")
    error_msg = (resource.get("error_message") or "").lower()
    sync_status = (resource.get("sync_status") or "").lower()

    # --- Projects: age + sync failure ---
    if resource_type == "projects":
        if not modified_str:
            return "Unknown"
        try:
            modified_dt = datetime.fromisoformat(modified_str.replace("Z", "+00:00"))
            one_year_ago = datetime.now(tz=timezone.utc) - timedelta(days=365)
            is_old = modified_dt < one_year_ago
        except (ValueError, TypeError):
            is_old = False

        sync_failed = sync_status in ("failed", "error")
        if is_old and sync_failed:
            return "Probably Stale"
        return "Active" if modified_str else "Unknown"

    # --- Required-field check (all non-project resource types) ---
    required_fields = _REQUIRED_FIELDS_BY_TYPE.get(resource_type, [])
    for meta_key, _label in required_fields:
        value = resource.get(meta_key)
        if not value or value == "N/A":
            return "Probably Stale"

    # --- Error-message pattern check ---
    if error_msg:
        for pattern in _MISSING_DEPENDENCY_PATTERNS:
            if pattern in error_msg:
                return "Probably Stale"

    if not modified_str:
        return "Unknown"
    return "Active"


# ---------------------------------------------------------------------------
# Organization summary builder (handles all statuses correctly)
# ---------------------------------------------------------------------------
def _build_full_org_summary(
    org_mapper: OrganizationMapper,
    resources: list[dict],
) -> dict:
    """Build organization summary handling completed/failed/skipped/pending correctly."""
    from collections import defaultdict

    org_summary: dict = defaultdict(lambda: {
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "pending": 0,
        "total": 0,
        "resource_types": set(),
        "resources": [],
    })

    for resource in resources:
        resource_type = resource.get("resource_type")
        source_id = resource.get("source_id")
        status = resource.get("status", "failed")

        if not resource_type or source_id is None:
            continue

        org_name = org_mapper.get_organization_name(resource_type, source_id)
        summary = org_summary[org_name]

        if status == "completed":
            summary["completed"] += 1
        elif status == "skipped":
            summary["skipped"] += 1
        elif status == "pending":
            summary["pending"] += 1
        else:
            summary["failed"] += 1
        summary["total"] += 1
        summary["resource_types"].add(resource_type)
        summary["resources"].append(resource)

    return dict(org_summary)


# ---------------------------------------------------------------------------
# CLI Command
# ---------------------------------------------------------------------------
@click.command(name="enhanced-report")
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file path (default: reports/org-failures-enhanced.{ext})",
)
@click.option(
    "--resource-type",
    "-r",
    type=str,
    help="Generate report for specific resource type only",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["html", "markdown", "csv"], case_sensitive=False),
    default="html",
    help="Output format (default: html)",
)
@click.option(
    "--organization",
    "--org",
    type=str,
    help="Generate report for a single organization only (case-insensitive match)",
)
@pass_context
@requires_config
@handle_errors
def generate_enhanced_report(
    ctx: MigrationContext,
    output: str | None,
    resource_type: str | None,
    output_format: str,
    organization: str | None,
) -> None:
    """Generate enhanced migration report with error analysis and metadata enrichment.

    \b
    Produces an interactive report that combines migration state (from the
    SQLite database) with resource metadata extracted from the source AAP
    export files.  The report includes:

    \b
    FEATURES
      - Dynamic error classification with human-readable explanations
      - Resource metadata: Last Modified, Ownership, Last Job Run, Sync Status
      - Resource staleness detection (Active / Probably Stale / Unknown)
      - Interactive HTML with filtering, sorting, column resizing, pagination
      - Detail modal with full error context per resource
      - CSV/Markdown exports for offline analysis

    \b
    OUTPUT FORMATS
      html      Interactive single-file HTML (default, recommended)
      markdown  Static markdown tables, suitable for Git/Wiki
      csv       Flat CSV for spreadsheet analysis

    Examples:

        # Generate enhanced HTML report (interactive)
        aap-bridge enhanced-report

        # Custom output path
        aap-bridge enhanced-report --output /tmp/enhanced-report.html

        # Report for specific resource type
        aap-bridge enhanced-report --resource-type job_templates

        # Generate markdown report
        aap-bridge enhanced-report --format markdown

        # Generate CSV report
        aap-bridge enhanced-report --format csv

        # Report for a single organization
        aap-bridge enhanced-report --organization "Organisation-1"

        # Single organization in CSV format
        aap-bridge enhanced-report --org "Organisation-1" --format csv
    """
    echo_info("Generating enhanced migration report (V2)...")

    # Set default output path
    if not output:
        extension_map = {"html": "html", "markdown": "md", "csv": "csv"}
        ext = extension_map.get(output_format, "html")
        if organization:
            safe_org = re.sub(r"[^\w\-]", "_", organization)
            output = f"{ctx.config.paths.report_dir}/org-{safe_org}-enhanced.{ext}"
        else:
            output = f"{ctx.config.paths.report_dir}/org-failures-enhanced.{ext}"

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        migration_state = ctx.migration_state
        export_dir = Path(ctx.config.paths.export_dir)
        transform_dir = Path(ctx.config.paths.transform_dir)

        # Step 1: Load organization mapper
        echo_info("Loading organization mappings...")
        org_mapper = OrganizationMapper(export_dir, transform_dir)

        # Step 2: Query all resources from database (all statuses)
        echo_info("Querying all resources from migration state...")
        all_resources = []

        with get_session(migration_state.database_url) as session:
            query = session.query(MigrationProgress).filter(
                MigrationProgress.phase == "import"
            )
            if resource_type:
                query = query.filter(MigrationProgress.resource_type == resource_type)

            for record in query.all():
                all_resources.append({
                    "resource_type": record.resource_type,
                    "source_id": record.source_id,
                    "source_name": record.source_name,
                    "status": record.status,
                    "error_message": record.error_message,
                    "phase": record.phase,
                })

        echo_info(f"Found {len(all_resources)} total resources")

        # Step 3: Build organization summary (custom logic to handle all statuses)
        echo_info("Mapping resources to organizations...")
        org_summary = _build_full_org_summary(org_mapper, all_resources)
        echo_info(f"Mapped to {len(org_summary)} organizations")

        # Step 3b: Filter by organization if requested
        if organization:
            org_lower = organization.lower()
            matched = {
                name: data for name, data in org_summary.items()
                if name.lower() == org_lower
            }
            if not matched:
                available = sorted(org_summary.keys())
                partial = [n for n in available if org_lower in n.lower()]
                if partial:
                    echo_error(
                        f"Organization '{organization}' not found. Similar: {', '.join(partial[:5])}"
                    )
                else:
                    echo_error(
                        f"Organization '{organization}' not found. "
                        f"Available ({len(available)}): {', '.join(available[:10])}{'...' if len(available) > 10 else ''}"
                    )
                raise click.ClickException(f"Organization '{organization}' not found")
            org_summary = matched
            echo_info(f"Filtered to organization: {list(org_summary.keys())[0]}")

        # Step 4: Build export metadata lookup
        echo_info("Loading export metadata (Last Modified, Modified By, Last Run, etc.)...")
        export_lookup = _build_export_lookup(export_dir)
        echo_info(f"Loaded metadata for {len(export_lookup)} resources")

        # Step 5: Enrich resources with error keys, export metadata, and org_name
        echo_info("Enriching data with dynamic error classification and metadata...")
        error_counter: Counter = Counter()

        for org_name, summary in org_summary.items():
            for resource in summary["resources"]:
                # Dynamic error classification
                error_msg = resource.get("error_message") or ""
                resource["error_key"] = normalize_error_key(error_msg)
                if resource["error_key"]:
                    error_counter[resource["error_key"]] += 1

                # Stamp org_name for global search
                resource["org_name"] = org_name

                # Export metadata enrichment
                rt = resource["resource_type"]
                sid = resource["source_id"]
                meta = export_lookup.get((rt, sid), {})
                resource["modified"] = meta.get("modified", "")
                resource["created_by"] = meta.get("created_by", "N/A")
                resource["modified_by"] = meta.get("modified_by", "N/A")
                resource["created"] = meta.get("created", "")
                resource["last_job_run"] = meta.get("last_job_run", "")
                resource["last_job_failed"] = meta.get("last_job_failed")
                resource["next_job_run"] = meta.get("next_job_run", "")
                resource["sync_status"] = meta.get("sync_status", "")
                resource["last_update_failed"] = meta.get("last_update_failed")

                for extra_key in (
                    "playbook", "project_name", "inventory_name",
                    "credential_names", "organization_name",
                    "scm_type", "scm_url", "scm_branch",
                ):
                    if extra_key in meta:
                        resource[extra_key] = meta[extra_key]

                hr_template = _HUMAN_READABLE_ERRORS.get(resource.get("error_key", ""))
                if hr_template:
                    resource["error_explanation"] = hr_template.format_map(
                        defaultdict(lambda: "N/A", resource)
                    )
                else:
                    ek = resource.get("error_key", "")
                    if ek.startswith("API: variables_needed_to_start"):
                        resource["error_explanation"] = _HUMAN_READABLE_ERRORS[
                            "API: variables_needed_to_start"
                        ].format_map(defaultdict(lambda: "N/A", resource))
                    elif ek.startswith("API: Field required"):
                        resource["error_explanation"] = (
                            f"A required field is missing for '{resource.get('source_name', 'N/A')}'."
                        )
                    elif ek.startswith("Validation:"):
                        resource["error_explanation"] = (
                            f"Validation failed for '{resource.get('source_name', 'N/A')}': {ek.replace('Validation: ', '')}."
                        )
                    elif ek.startswith("Duplicate "):
                        resource["error_explanation"] = (
                            f"A duplicate {ek.replace('Duplicate ', '')} was found in the source AAP."
                            " Only the first occurrence was imported."
                        )

                # Determine resource staleness (needs metadata + error_message populated)
                resource["resource_status"] = _determine_resource_status(resource)

                # Truncate long error messages for report size
                err = resource.get("error_message")
                if err and len(err) > 500:
                    resource["error_message"] = err[:500] + "..."

        echo_info(f"Classified into {len(error_counter)} unique error types")

        # Step 6: Generate report in chosen format
        if output_format == "html":
            echo_info("Generating enhanced HTML report...")
            report_content = _generate_enhanced_html(org_summary, migration_state, export_dir, org_mapper, source_config=ctx.config.source)
        elif output_format == "markdown":
            echo_info("Generating enhanced Markdown report...")
            report_content = _format_enhanced_markdown(org_summary, migration_state, error_counter)
        elif output_format == "csv":
            echo_info("Generating enhanced CSV report...")
            report_content = _format_enhanced_csv(org_summary)
        else:
            report_content = _generate_enhanced_html(org_summary, migration_state, export_dir, org_mapper, source_config=ctx.config.source)

        output_path.write_text(report_content, encoding="utf-8")

        file_size_mb = output_path.stat().st_size / (1024 * 1024)
        echo_success(f"Enhanced report generated: {output} ({file_size_mb:.1f} MB)")

        # Print summary to console
        _print_enhanced_summary(org_summary, error_counter)

    except Exception as e:
        echo_error(f"Failed to generate enhanced report: {e}")
        logger.error("Enhanced report generation failed", error=str(e), exc_info=True)
        raise click.ClickException(str(e)) from e


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------
def _print_enhanced_summary(org_summary: dict, error_counter: Counter) -> None:
    """Print a comprehensive summary to the console."""
    click.echo()
    click.echo("=" * 100)
    click.echo("ENHANCED MIGRATION SUMMARY")
    click.echo("=" * 100)

    sorted_orgs = sorted(
        org_summary.items(),
        key=lambda x: x[1]["total"],
        reverse=True,
    )

    total_completed = sum(s["completed"] for _, s in sorted_orgs)
    total_failed = sum(s["failed"] for _, s in sorted_orgs)
    total_skipped = sum(s["skipped"] for _, s in sorted_orgs)
    total_pending = sum(s["pending"] for _, s in sorted_orgs)
    total_all = sum(s["total"] for _, s in sorted_orgs)
    stalled_count = 0
    for _, summary in sorted_orgs:
        for r in summary["resources"]:
            if r.get("resource_status") == "Probably Stale":
                stalled_count += 1

    rate = round((total_completed / total_all) * 100) if total_all > 0 else 0

    click.echo(f"  Organizations: {len(sorted_orgs)}")
    click.echo(f"  Total resources: {total_all}")
    click.echo(f"  Completed: {total_completed} | Failed: {total_failed} | Skipped: {total_skipped} | Pending: {total_pending}")
    click.echo(f"  Probably Stale: {stalled_count}")
    click.echo(f"  Success rate: {rate}%")
    click.echo()

    for org_name, summary in sorted_orgs[:20]:
        failed = summary["failed"]
        skipped = summary["skipped"]
        pending = summary["pending"]
        total = summary["total"]

        if failed > 0:
            status = click.style("HAS FAILURES", fg="red", bold=True)
        elif skipped > 0:
            status = click.style("HAS SKIPPED", fg="cyan", bold=True)
        elif pending > 0:
            status = click.style("PENDING", fg="yellow", bold=True)
        else:
            status = click.style("OK", fg="green")

        click.echo(
            f"  {org_name:40s} | Failed: {failed:4d} | Skipped: {skipped:4d} | Pending: {pending:4d} | Total: {total:4d} | {status}"
        )

    if len(sorted_orgs) > 20:
        click.echo(f"  ... and {len(sorted_orgs) - 20} more organizations")

    click.echo()
    click.echo("Top 10 error types:")
    for key, count in error_counter.most_common(10):
        click.echo(f"  [{count:6d}] {key}")

    click.echo("=" * 100)
    click.echo()


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------
def _format_enhanced_markdown(
    org_summary: dict,
    migration_state,
    error_counter: Counter,
) -> str:
    """Generate enhanced markdown report with organization breakdowns."""
    lines = [
        "# AAP Migration - Enhanced Report",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Migration ID:** {migration_state.migration_id}",
        "",
        "---",
        "",
    ]

    sorted_orgs = sorted(
        org_summary.items(),
        key=lambda x: x[1]["total"],
        reverse=True,
    )

    total_completed = sum(s["completed"] for _, s in sorted_orgs)
    total_failed = sum(s["failed"] for _, s in sorted_orgs)
    total_skipped = sum(s["skipped"] for _, s in sorted_orgs)
    total_pending = sum(s["pending"] for _, s in sorted_orgs)
    total_all = sum(s["total"] for _, s in sorted_orgs)
    stalled_count = sum(
        1 for _, s in sorted_orgs
        for r in s["resources"]
        if r.get("resource_status") == "Probably Stale"
    )
    rate = round((total_completed / total_all) * 100) if total_all > 0 else 0

    lines.extend([
        "## Global Summary",
        "",
        f"- **Organizations:** {len(sorted_orgs)}",
        f"- **Total resources:** {total_all}",
        f"- **Completed:** {total_completed}",
        f"- **Failed:** {total_failed}",
        f"- **Skipped:** {total_skipped}",
        f"- **Pending:** {total_pending}",
        f"- **Probably Stale:** {stalled_count}",
        f"- **Success rate:** {rate}%",
        "",
    ])

    # Error classification summary
    if error_counter:
        lines.extend([
            "## Error Classification Summary",
            "",
            "| Error Type | Count |",
            "|------------|-------|",
        ])
        for key, count in error_counter.most_common(30):
            key_escaped = key.replace("|", "\\|")
            lines.append(f"| {key_escaped} | {count} |")
        lines.append("")

    # Organization summary table
    lines.extend([
        "## Summary by Organization",
        "",
        "| Organization | Total | Completed | Failed | Skipped | Pending | Prob. Stale | Success Rate |",
        "|--------------|-------|-----------|--------|---------|---------|-------------|--------------|",
    ])

    for org_name, summary in sorted_orgs:
        completed = summary["completed"]
        failed = summary["failed"]
        skipped = summary["skipped"]
        pending = summary["pending"]
        total = summary["total"]
        org_stalled = sum(1 for r in summary["resources"] if r.get("resource_status") == "Probably Stale")
        org_rate = round((completed / total) * 100) if total > 0 else 0

        failed_str = f"**{failed}**" if failed > 0 else str(failed)
        skipped_str = f"**{skipped}**" if skipped > 0 else str(skipped)
        pending_str = f"**{pending}**" if pending > 0 else str(pending)

        lines.append(
            f"| {org_name} | {total} | {completed} | {failed_str} | {skipped_str} | {pending_str} | {org_stalled} | {org_rate}% |"
        )

    lines.append("")
    lines.append("---")
    lines.append("")

    # Detailed sections per organization (only orgs with issues)
    for org_name, summary in sorted_orgs:
        if summary["failed"] == 0 and summary["skipped"] == 0 and summary["pending"] == 0:
            continue

        lines.append(f"## {org_name}")
        lines.append("")
        lines.append(f"- Completed: {summary['completed']}")
        lines.append(f"- Failed: {summary['failed']}")
        lines.append(f"- Skipped: {summary['skipped']}")
        lines.append(f"- Pending: {summary['pending']}")
        lines.append(f"- Resource Types: {', '.join(sorted(summary['resource_types']))}")
        lines.append("")

        by_type: dict[str, list] = {}
        for resource in summary["resources"]:
            if resource["status"] not in ("failed", "skipped", "pending"):
                continue
            rtype = resource["resource_type"]
            if rtype not in by_type:
                by_type[rtype] = []
            by_type[rtype].append(resource)

        for rtype in sorted(by_type.keys()):
            resources = by_type[rtype]
            lines.append(f"### {rtype} ({len(resources)})")
            lines.append("")
            lines.append("| Source ID | Name | Status | Error | Error Explanation | Resource Status | Last Modified |")
            lines.append("|-----------|------|--------|-------|-------------------|-----------------|---------------|")

            for resource in resources:
                source_id = resource["source_id"]
                source_name = resource.get("source_name", "N/A")
                status = resource["status"]
                error_key = resource.get("error_key", "")
                error_key = error_key.replace("|", "\\|")
                explanation = resource.get("error_explanation", "")
                explanation = explanation.replace("|", "\\|")
                res_status = resource.get("resource_status", "Unknown")
                modified = resource.get("modified", "")
                if modified:
                    modified = modified.split("T")[0]

                lines.append(f"| {source_id} | {source_name} | {status} | {error_key} | {explanation} | {res_status} | {modified} |")

            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CSV report
# ---------------------------------------------------------------------------
def _format_enhanced_csv(org_summary: dict) -> str:
    """Generate enhanced CSV report with all enriched data."""
    output = StringIO()
    writer = csv_module.writer(output)

    writer.writerow([
        "Organization",
        "Resource Type",
        "Source ID",
        "Name",
        "Migration Status",
        "Error Classification",
        "Error Explanation",
        "Error Message",
        "Resource Status in AAP",
        "Last Modified",
        "Created By",
        "Last Modified By",
        "Created",
        "Last Job Run",
        "Last Job Failed",
        "Next Job Run",
        "Sync Status",
    ])

    sorted_orgs = sorted(
        org_summary.items(),
        key=lambda x: x[1]["total"],
        reverse=True,
    )

    for org_name, summary in sorted_orgs:
        for resource in summary["resources"]:
            writer.writerow([
                org_name,
                resource["resource_type"],
                resource["source_id"],
                resource.get("source_name", "N/A"),
                resource["status"],
                resource.get("error_key", ""),
                resource.get("error_explanation", ""),
                resource.get("error_message", ""),
                resource.get("resource_status", "Unknown"),
                resource.get("modified", ""),
                resource.get("created_by", "N/A"),
                resource.get("modified_by", "N/A"),
                resource.get("created", ""),
                resource.get("last_job_run", ""),
                resource.get("last_job_failed", ""),
                resource.get("next_job_run", ""),
                resource.get("sync_status", ""),
            ])

    return output.getvalue()


# ---------------------------------------------------------------------------
# HTML Generation
# ---------------------------------------------------------------------------
def _generate_enhanced_html(org_summary: dict, migration_state, export_dir: Path, org_mapper: OrganizationMapper, source_config=None) -> str:
    """Generate the enhanced interactive HTML report."""
    from html import escape

    user_emails = _build_user_email_lookup(export_dir, org_mapper, source_config)

    json_data = {
        "metadata": {
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "migration_id": str(migration_state.migration_id),
        },
        "organizations": {},
        "user_emails": user_emails,
    }

    for org_name, summary in org_summary.items():
        completed = sum(1 for r in summary["resources"] if r["status"] == "completed")
        failed = sum(1 for r in summary["resources"] if r["status"] == "failed")
        skipped = sum(1 for r in summary["resources"] if r["status"] == "skipped")
        pending = sum(1 for r in summary["resources"] if r["status"] == "pending")

        json_data["organizations"][org_name] = {
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "pending": pending,
            "total": summary["total"],
            "resource_types": sorted(list(summary["resource_types"])),
            "resources": summary["resources"],
        }

    data_json = json.dumps(json_data, ensure_ascii=False, separators=(",", ":"))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AAP Migration - Enhanced Report</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 20px; min-height: 100vh; }}
        .container {{ max-width: 1900px; margin: 0 auto; background: white; border-radius: 12px; box-shadow: 0 10px 40px rgba(0,0,0,0.2); overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; }}
        .header h1 {{ font-size: 2em; margin-bottom: 10px; }}
        .header .metadata {{ opacity: 0.9; font-size: 0.9em; }}
        .tabs {{ display: flex; background: #f8f9fa; border-bottom: 3px solid #e9ecef; overflow-x: auto; }}
        .tab {{ padding: 15px 30px; cursor: pointer; border: none; background: transparent; font-size: 1em; font-weight: 600; color: #6c757d; transition: all 0.3s; border-bottom: 3px solid transparent; margin-bottom: -3px; white-space: nowrap; }}
        .tab:hover {{ background: #e9ecef; color: #495057; }}
        .tab.active {{ color: #667eea; border-bottom-color: #667eea; background: white; }}
        .controls {{ background: #fff; padding: 20px 30px; border-bottom: 2px solid #e9ecef; display: flex; gap: 15px; flex-wrap: wrap; align-items: flex-end; }}
        .controls.hidden {{ display: none; }}
        .control-group {{ display: flex; flex-direction: column; gap: 5px; }}
        .control-group label {{ font-size: 0.85em; font-weight: 600; color: #495057; text-transform: uppercase; letter-spacing: 0.5px; }}
        select, input[type="text"] {{ padding: 10px 15px; border: 2px solid #dee2e6; border-radius: 6px; font-size: 14px; min-width: 200px; transition: all 0.3s; }}
        select:focus, input[type="text"]:focus {{ outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1); }}
        .stats {{ padding: 20px 30px; background: #fff; display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 15px; }}
        .stat-card {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 8px; text-align: center; }}
        .stat-card.success {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
        .stat-card.failed {{ background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); }}
        .stat-card.skipped {{ background: linear-gradient(135deg, #ffecd2 0%, #fcb69f 100%); }}
        .stat-card.stalled {{ background: linear-gradient(135deg, #fc5c7d 0%, #6a82fb 100%); }}
        .stat-card.pending {{ background: linear-gradient(135deg, #667eea 0%, #4a6cf7 100%); }}
        .stat-card .value {{ font-size: 2.5em; font-weight: bold; margin-bottom: 5px; }}
        .stat-card .label {{ font-size: 0.9em; opacity: 0.9; }}
        .content {{ padding: 30px; min-height: 400px; overflow-x: auto; }}
        .content.hidden {{ display: none; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 0.85em; table-layout: fixed; }}
        th {{ background: #667eea; color: white; padding: 12px 8px; text-align: left; font-weight: 600; position: sticky; top: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; position: relative; user-select: none; }}
        th.sortable {{ cursor: pointer; }}
        th.sortable:hover {{ background: #5a6fd6; }}
        th .sort-arrow {{ margin-left: 4px; font-size: 0.8em; }}
        th .resizer {{ position: absolute; right: 0; top: 0; width: 5px; height: 100%; cursor: col-resize; background: rgba(255,255,255,0.3); }}
        th .resizer:hover, th .resizer.active {{ background: rgba(255,255,255,0.7); }}
        td {{ padding: 10px 8px; border-bottom: 1px solid #e9ecef; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        tr:hover {{ background: #f8f9fa; }}
        tr.clickable {{ cursor: pointer; }}
        .status-completed {{ background: #38ef7d; color: white; padding: 4px 8px; border-radius: 4px; font-size: 0.85em; font-weight: 600; }}
        .status-failed {{ background: #f5576c; color: white; padding: 4px 8px; border-radius: 4px; font-size: 0.85em; font-weight: 600; }}
        .status-skipped {{ background: #fcb69f; color: #333; padding: 4px 8px; border-radius: 4px; font-size: 0.85em; font-weight: 600; }}
        .status-pending {{ background: #007bff; color: white; padding: 4px 8px; border-radius: 4px; font-size: 0.85em; font-weight: 600; }}
        .resource-status-active {{ background: #d4edda; color: #155724; padding: 4px 8px; border-radius: 4px; font-size: 0.85em; font-weight: 600; }}
        .resource-status-stale {{ background: #f8d7da; color: #721c24; padding: 4px 8px; border-radius: 4px; font-size: 0.85em; font-weight: 600; }}
        .resource-status-unknown {{ background: #e2e3e5; color: #383d41; padding: 4px 8px; border-radius: 4px; font-size: 0.85em; font-weight: 600; }}
        .success-rate {{ font-weight: 600; padding: 4px 8px; border-radius: 4px; }}
        .success-rate.high {{ background: #d4edda; color: #155724; }}
        .success-rate.medium {{ background: #fff3cd; color: #856404; }}
        .success-rate.low {{ background: #f8d7da; color: #721c24; }}
        .last-run-cell {{ font-size: 0.85em; white-space: nowrap; }}
        .last-run-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }}
        .last-run-dot.success {{ background: #38ef7d; }}
        .last-run-dot.failed {{ background: #f5576c; }}
        .last-run-dot.unknown {{ background: #dee2e6; }}
        .pagination {{ display: flex; justify-content: center; align-items: center; gap: 10px; padding: 20px; margin-top: 20px; }}
        .pagination.hidden {{ display: none; }}
        .pagination button {{ padding: 8px 16px; border: 2px solid #667eea; background: white; color: #667eea; border-radius: 6px; cursor: pointer; font-weight: 600; transition: all 0.3s; }}
        .pagination button:hover:not(:disabled) {{ background: #667eea; color: white; }}
        .pagination button:disabled {{ opacity: 0.3; cursor: not-allowed; }}
        .pagination .page-info {{ padding: 0 15px; font-weight: 600; color: #495057; }}
        .resource-type-section {{ margin-bottom: 30px; }}
        .resource-type-section h3 {{ color: #495057; padding: 10px 0; border-bottom: 2px solid #e9ecef; margin-bottom: 15px; }}
        .error-cell {{ word-wrap: break-word; overflow-wrap: break-word; font-size: 0.85em; color: #495057; cursor: pointer; white-space: normal; }}
        .error-cell:hover {{ color: #667eea; text-decoration: underline; }}
        .name-link {{ cursor: pointer; color: #333; }}
        .name-link:hover {{ color: #667eea; text-decoration: underline; }}
        .modified-cell {{ white-space: nowrap; font-size: 0.85em; }}
        .no-data {{ text-align: center; padding: 60px 20px; color: #6c757d; }}
        .error-key-badge {{ display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 0.8em; font-weight: 600; background: #e9ecef; color: #495057; white-space: normal; word-wrap: break-word; overflow-wrap: break-word; }}
        .btn-toggle {{ padding: 8px 14px; border: 2px solid #667eea; background: white; color: #667eea; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 13px; transition: all 0.3s; white-space: nowrap; }}
        .btn-toggle:hover {{ background: #667eea; color: white; }}
        .btn-toggle.active {{ background: #667eea; color: white; }}
        .size-controls {{ display: flex; gap: 10px; align-items: center; padding: 10px 30px; background: #f8f9fa; border-bottom: 1px solid #e9ecef; }}
        .size-controls.hidden {{ display: none; }}
        .size-controls label {{ font-size: 0.85em; font-weight: 600; color: #495057; }}
        .size-controls input[type="range"] {{ width: 120px; }}
        .size-controls span {{ font-size: 0.8em; color: #6c757d; min-width: 40px; }}
        .summary-search {{ padding: 15px 30px; background: #f8f9fa; border-bottom: 1px solid #e9ecef; }}
        .summary-search.hidden {{ display: none; }}
        .summary-search input {{ padding: 10px 15px; border: 2px solid #dee2e6; border-radius: 6px; font-size: 14px; width: 100%; max-width: 400px; transition: all 0.3s; }}
        .summary-search input:focus {{ outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1); }}
        .group-row {{ cursor: pointer; }}
        .group-row:hover {{ background: #e9ecef !important; }}
        .group-row td {{ font-weight: 600; }}
        .group-count {{ background: #667eea; color: white; padding: 2px 8px; border-radius: 10px; font-size: 0.85em; }}
        .modal-overlay {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 10000; justify-content: center; align-items: center; }}
        .modal-overlay.visible {{ display: flex; }}
        .modal {{ background: white; border-radius: 12px; padding: 30px; max-width: 800px; width: 90%; max-height: 80vh; overflow-y: auto; box-shadow: 0 20px 60px rgba(0,0,0,0.3); position: relative; }}
        .modal-close {{ position: absolute; top: 15px; right: 20px; background: none; border: none; font-size: 1.5em; cursor: pointer; color: #6c757d; line-height: 1; }}
        .modal-close:hover {{ color: #333; }}
        .modal h2 {{ margin-bottom: 20px; color: #333; font-size: 1.3em; }}
        .modal-field {{ margin-bottom: 15px; }}
        .modal-field .field-label {{ font-weight: 600; color: #667eea; font-size: 0.85em; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }}
        .modal-field .field-value {{ background: #f8f9fa; padding: 10px 15px; border-radius: 6px; font-size: 0.9em; word-break: break-all; border: 1px solid #e9ecef; }}
        .modal-field .field-value.error-text {{ color: #dc3545; background: #fff5f5; border-color: #f8d7da; }}
        .modal-field .field-value.explanation-box {{ color: #0c5460; background: #d1ecf1; border-color: #bee5eb; font-style: italic; }}
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>AAP Migration - Enhanced Report</h1>
        <div class="metadata">
            Generated: {escape(json_data["metadata"]["generated"])} |
            Migration ID: {escape(json_data["metadata"]["migration_id"])} |
            Dynamic error classification &bull; Sortable &bull; Resizable &bull; Click for details
        </div>
    </div>
    <div class="tabs">
        <button class="tab active" data-tab="summary">&#x1F4CA; Overview</button>
        <button class="tab" data-tab="successful">&#x2705; Successful</button>
        <button class="tab" data-tab="failures">&#x274C; Failed</button>
        <button class="tab" data-tab="skipped">&#x23ED;&#xFE0F; Skipped</button>
        <button class="tab" data-tab="pending">&#x23F3; Pending</button>
        <button class="tab" data-tab="complete">&#x1F4CB; Org Summary</button>
    </div>
    <div class="controls hidden" id="controls">
        <div class="control-group"><label for="orgSelect">Organization</label><select id="orgSelect"><option value="">All Organizations</option></select></div>
        <div class="control-group"><label for="resourceTypeFilter">Resource Type</label><select id="resourceTypeFilter"><option value="">All Types</option></select></div>
        <div class="control-group"><label for="errorFilter">Error</label><select id="errorFilter"><option value="">All Errors</option></select></div>
        <div class="control-group"><label for="migrationStatusFilter">Migration Status</label><select id="migrationStatusFilter"><option value="">All</option><option value="completed">Completed</option><option value="failed">Failed</option><option value="skipped">Skipped</option><option value="pending">Pending</option></select></div>
        <div class="control-group"><label for="statusFilter">Resource Status in AAP</label><select id="statusFilter"><option value="">All</option><option value="Active">Active</option><option value="Probably Stale">Probably Stale</option><option value="Unknown">Unknown</option></select></div>
        <div class="control-group"><label for="searchInput">Search</label><input type="text" id="searchInput" placeholder="Search by name, ID, org, or error..."></div>
        <div class="control-group"><label>&nbsp;</label>
            <div style="display:flex;gap:8px;">
                <button id="groupToggle" class="btn-toggle" title="Group failures by error pattern">Group Errors</button>
                <button class="btn-toggle" onclick="exportCSV()">Export CSV</button>
                <button class="btn-toggle" onclick="exportCSVWithEmails()">Export CSV + Emails</button>
            </div>
        </div>
    </div>
    <div class="size-controls hidden" id="sizeControls">
        <label>Row height:</label><input type="range" id="rowHeightSlider" min="24" max="80" value="40"><span id="rowHeightValue">40px</span>
        <label style="margin-left:20px;">Font size:</label><input type="range" id="fontSizeSlider" min="10" max="18" value="13"><span id="fontSizeValue">13px</span>
    </div>
    <div class="summary-search hidden" id="summarySearch">
        <input type="text" id="summarySearchInput" placeholder="Search organizations...">
    </div>
    <div class="stats" id="statsContainer"></div>
    <div class="content" id="summaryContent"></div>
    <div class="content hidden" id="successfulContent"></div>
    <div class="content hidden" id="failuresContent"></div>
    <div class="content hidden" id="skippedContent"></div>
    <div class="content hidden" id="pendingContent"></div>
    <div class="content hidden" id="completeContent"></div>
    <div class="pagination hidden" id="paginationContainer">
        <button id="prevPage">&larr; Previous</button>
        <span class="page-info" id="pageInfo">Page 1 of 1</span>
        <button id="nextPage">Next &rarr;</button>
    </div>
</div>
<div class="modal-overlay" id="errorModal">
    <div class="modal">
        <button class="modal-close" onclick="closeModal()">&times;</button>
        <h2 id="modalTitle">Resource Detail</h2>
        <div id="modalBody"></div>
    </div>
</div>
<script>
const DATA = {data_json};
let currentTab = 'summary', currentOrg = '', currentPage = 1, filteredData = [];
const itemsPerPage = 100;
let tableRowHeight = 40, tableFontSize = 13;
let _allResourcesCache = null;
let _searchDebounceTimer = null;
let viewMode = 'flat';
let currentSort = {{ column: '', direction: 'asc' }};

function getAllResources() {{
    if (!_allResourcesCache) {{
        _allResourcesCache = [];
        Object.values(DATA.organizations).forEach(org => {{
            org.resources.forEach(r => _allResourcesCache.push(r));
        }});
    }}
    return _allResourcesCache;
}}

function getAllResourceTypes() {{
    const types = new Set();
    Object.values(DATA.organizations).forEach(org => {{
        org.resource_types.forEach(t => types.add(t));
    }});
    return Array.from(types).sort();
}}

function formatTs(ts) {{
    if (!ts) return '<span style="color:#adb5bd;">—</span>';
    return new Date(ts).toISOString().replace('T', ' ').substring(0, 16);
}}

function formatLastRun(resource) {{
    const ts = resource.last_job_run;
    if (!ts) return '<span class="last-run-cell" style="color:#adb5bd;">—</span>';
    const dateStr = formatTs(ts);
    const failed = resource.last_job_failed;
    let dotClass = 'unknown';
    if (failed === true) dotClass = 'failed';
    else if (failed === false) dotClass = 'success';
    return '<span class="last-run-cell"><span class="last-run-dot ' + dotClass + '"></span>' + dateStr + '</span>';
}}

function formatSyncStatus(resource) {{
    const st = resource.sync_status;
    if (!st && resource.last_update_failed === undefined) return '<span style="color:#adb5bd;">—</span>';
    let color = '#adb5bd';
    if (st === 'successful') color = '#28a745';
    else if (st === 'failed' || st === 'error') color = '#dc3545';
    else if (st === 'never updated') color = '#6c757d';
    else if (st === 'running') color = '#007bff';
    else if (st === 'canceled') color = '#ffc107';
    const label = st || (resource.last_update_failed ? 'failed' : 'ok');
    return '<span style="color:' + color + ';font-weight:600;">' + label + '</span>';
}}

function getErrorPattern(msg) {{
    if (!msg) return '(no message)';
    return msg.replace(/'[^']*'/g, "'...'").replace(/\\b\\d{{4,}}\\b/g, 'NNN').substring(0, 100);
}}

function applySortToFiltered() {{
    if (!currentSort.column) return;
    const col = currentSort.column;
    const dir = currentSort.direction === 'asc' ? 1 : -1;
    filteredData.sort((a, b) => {{
        let va = a[col], vb = b[col];
        if (va == null && vb == null) return 0;
        if (va == null) return 1;
        if (vb == null) return -1;
        if (typeof va === 'string') va = va.toLowerCase();
        if (typeof vb === 'string') vb = vb.toLowerCase();
        if (va < vb) return -dir;
        if (va > vb) return dir;
        return 0;
    }});
}}

function sortData(column) {{
    if (currentSort.column === column) {{
        currentSort.direction = currentSort.direction === 'asc' ? 'desc' : 'asc';
    }} else {{
        currentSort.column = column;
        currentSort.direction = 'asc';
    }}
    applySortToFiltered();
    currentPage = 1;
    renderPage();
}}

function init() {{ populateOrgDropdown(); renderSummaryTab(); attachEventListeners(); }}

function populateOrgDropdown() {{
    const select = document.getElementById('orgSelect');
    Object.keys(DATA.organizations).sort((a, b) => DATA.organizations[b].total - DATA.organizations[a].total).forEach(org => {{
        const o = document.createElement('option'); o.value = org;
        const s = DATA.organizations[org];
        o.textContent = org + ' (Success: ' + (s.total > 0 ? Math.round((s.completed / s.total) * 100) : 0) + '%, Total: ' + s.total + ')';
        select.appendChild(o);
    }});
}}

function switchTab(tabName) {{
    currentTab = tabName; currentPage = 1; viewMode = 'flat';
    const gt = document.getElementById('groupToggle'); if(gt) gt.classList.remove('active');
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tabName));
    document.querySelectorAll('.content').forEach(c => c.classList.add('hidden'));
    const controls = document.getElementById('controls'), sc = document.getElementById('sizeControls'), ss = document.getElementById('summarySearch');
    if (tabName === 'summary') {{
        controls.classList.add('hidden'); sc.classList.add('hidden'); ss.classList.remove('hidden');
        document.getElementById('summaryContent').classList.remove('hidden');
        renderSummaryTab();
    }} else {{
        controls.classList.remove('hidden'); sc.classList.remove('hidden'); ss.classList.add('hidden');
        document.getElementById(tabName + 'Content').classList.remove('hidden');
        populateResourceTypeFilter(); populateErrorFilter();
        renderDetailTab();
    }}
}}

function renderSummaryTab() {{
    const searchTerm = (document.getElementById('summarySearchInput').value || '').toLowerCase();
    const orgs = Object.entries(DATA.organizations).filter(([name]) => {{
        if (!searchTerm) return true;
        return name.toLowerCase().includes(searchTerm);
    }});
    const tC = orgs.reduce((s,[_,d])=>s+d.completed,0), tF = orgs.reduce((s,[_,d])=>s+d.failed,0), tSk = orgs.reduce((s,[_,d])=>s+d.skipped,0), tP = orgs.reduce((s,[_,d])=>s+d.pending,0), tAll = orgs.reduce((s,[_,d])=>s+d.total,0);
    let tSt = 0; orgs.forEach(([_,o]) => o.resources.forEach(r => {{ if(r.resource_status==='Probably Stale') tSt++; }}));
    const sr = tAll > 0 ? Math.round((tC/tAll)*100) : 0;
    document.getElementById('statsContainer').innerHTML =
        '<div class="stat-card"><div class="value">' + orgs.length + '</div><div class="label">Organizations</div></div>' +
        '<div class="stat-card success"><div class="value">' + tC + '</div><div class="label">Successful</div></div>' +
        '<div class="stat-card failed"><div class="value">' + tF + '</div><div class="label">Failed</div></div>' +
        '<div class="stat-card skipped"><div class="value">' + tSk + '</div><div class="label">Skipped</div></div>' +
        '<div class="stat-card pending"><div class="value">' + tP + '</div><div class="label">Pending</div></div>' +
        '<div class="stat-card stalled"><div class="value">' + tSt + '</div><div class="label">Prob. Stale</div></div>' +
        '<div class="stat-card"><div class="value">' + sr + '%</div><div class="label">Success Rate</div></div>';
    const sorted = orgs.sort((a,b) => b[1].total - a[1].total);
    let h = '<table style="table-layout:auto"><thead><tr><th>Organization</th><th>Total</th><th>Successful</th><th>Failed</th><th>Skipped</th><th>Pending</th><th>Prob. Stale</th><th>Success Rate</th><th>Resource Types</th></tr></thead><tbody>';
    sorted.forEach(([n,s]) => {{
        const r = s.total>0?Math.round((s.completed/s.total)*100):0;
        const rc = r<50?'low':r<80?'medium':'high';
        const st = s.resources.filter(x=>x.resource_status==='Probably Stale').length;
        const ne = n.replace(/'/g, "\\\\'");
        h += '<tr class="clickable">' +
            '<td class="clickable" onclick="goToOrgTab(\\'' + ne + '\\',\\'complete\\')" title="View Org Summary"><strong>' + escapeHtml(n) + '</strong></td>' +
            '<td class="clickable" onclick="goToOrgTab(\\'' + ne + '\\',\\'complete\\')" title="View Org Summary">' + s.total + '</td>' +
            '<td class="clickable" onclick="goToOrgTab(\\'' + ne + '\\',\\'successful\\')" title="View Successful"><span class="status-completed">' + s.completed + '</span></td>' +
            '<td class="clickable" onclick="goToOrgTab(\\'' + ne + '\\',\\'failures\\')" title="View Failures"><span class="status-failed">' + s.failed + '</span></td>' +
            '<td class="clickable" onclick="goToOrgTab(\\'' + ne + '\\',\\'skipped\\')" title="View Skipped"><span class="status-skipped">' + s.skipped + '</span></td>' +
            '<td class="clickable" onclick="goToOrgTab(\\'' + ne + '\\',\\'pending\\')" title="View Pending"><span class="status-pending">' + s.pending + '</span></td>' +
            '<td>' + (st>0?'<span class="resource-status-stale">'+st+'</span>':'0') + '</td>' +
            '<td><span class="success-rate '+rc+'">' + r + '%</span></td>' +
            '<td style="font-size:0.85em">' + s.resource_types.join(', ') + '</td></tr>';
    }});
    h += '</tbody></table>'; document.getElementById('summaryContent').innerHTML = h; document.getElementById('paginationContainer').classList.add('hidden');
}}

function goToOrgTab(n, tab) {{ document.getElementById('orgSelect').value = n; currentOrg = n; populateResourceTypeFilter(); populateErrorFilter(); switchTab(tab); }}
function goToOrg(n) {{ goToOrgTab(n, 'complete'); }}

function populateResourceTypeFilter() {{
    const s = document.getElementById('resourceTypeFilter');
    const cv = s.value;
    s.innerHTML = '<option value="">All Types</option>';
    let types;
    if (currentOrg) {{ types = DATA.organizations[currentOrg].resource_types; }}
    else {{ types = getAllResourceTypes(); }}
    types.forEach(t => {{ const o = document.createElement('option'); o.value = t; o.textContent = t; s.appendChild(o); }});
    if (cv && types.includes(cv)) s.value = cv;
}}

function populateErrorFilter() {{
    const s = document.getElementById('errorFilter');
    s.innerHTML = '<option value="">All Errors</option>';
    let resources;
    if (currentOrg) {{ resources = DATA.organizations[currentOrg].resources; }}
    else {{ resources = getAllResources(); }}
    const rtf = document.getElementById('resourceTypeFilter').value;
    const counts = {{}};
    resources.forEach(r => {{
        if (r.error_key) {{
            if (rtf && r.resource_type !== rtf) return;
            counts[r.error_key] = (counts[r.error_key]||0)+1;
        }}
    }});
    Object.entries(counts).sort((a,b)=>b[1]-a[1]).forEach(([k,c]) => {{
        const o = document.createElement('option'); o.value = k; o.textContent = k + ' (' + c + ')'; s.appendChild(o);
    }});
}}

function renderDetailTab() {{
    let resources;
    if (currentOrg) {{ resources = DATA.organizations[currentOrg].resources; }}
    else {{ resources = getAllResources(); }}
    const rtf = document.getElementById('resourceTypeFilter').value;
    const ef = document.getElementById('errorFilter').value;
    const ms = document.getElementById('migrationStatusFilter').value;
    const sf = document.getElementById('statusFilter').value;
    const st = document.getElementById('searchInput').value.toLowerCase();
    let msf = [];
    if (ms) msf=[ms];
    else if (currentTab==='failures') msf=['failed'];
    else if (currentTab==='skipped') msf=['skipped'];
    else if (currentTab==='successful') msf=['completed'];
    else if (currentTab==='pending') msf=['pending'];
    else msf=['completed','failed','skipped','pending'];
    filteredData = resources.filter(r => {{
        if (!msf.includes(r.status)) return false;
        if (rtf && r.resource_type!==rtf) return false;
        if (ef && r.error_key!==ef) return false;
        if (sf && r.resource_status!==sf) return false;
        if (st) {{
            const mn = r.source_name&&r.source_name.toLowerCase().includes(st);
            const mi = r.source_id&&r.source_id.toString().includes(st);
            const me = r.error_message&&r.error_message.toLowerCase().includes(st);
            const mo = r.org_name&&r.org_name.toLowerCase().includes(st);
            const mlr = r.last_job_run&&r.last_job_run.toLowerCase().includes(st);
            const mss = r.sync_status&&r.sync_status.toLowerCase().includes(st);
            const mmd = r.modified&&r.modified.toLowerCase().includes(st);
            if (!mn&&!mi&&!me&&!mo&&!mlr&&!mss&&!mmd) return false;
        }}
        return true;
    }});
    const c = filteredData.filter(r=>r.status==='completed').length, f = filteredData.filter(r=>r.status==='failed').length, sk = filteredData.filter(r=>r.status==='skipped').length, pn = filteredData.filter(r=>r.status==='pending').length;
    const stl = filteredData.filter(r=>r.resource_status==='Probably Stale').length;
    const rate = filteredData.length>0?Math.round((c/filteredData.length)*100):0;
    const orgLabel = currentOrg ? escapeHtml(currentOrg) : 'All Organizations';
    document.getElementById('statsContainer').innerHTML =
        '<div class="stat-card"><div class="value" style="font-size:1.4em">' + orgLabel + '</div><div class="label">' + (currentOrg ? 'Selected Organization' : filteredData.length + ' matching resources') + '</div></div>' +
        '<div class="stat-card success"><div class="value">' + c + '</div><div class="label">Successful</div></div>' +
        '<div class="stat-card failed"><div class="value">' + f + '</div><div class="label">Failed</div></div>' +
        '<div class="stat-card skipped"><div class="value">' + sk + '</div><div class="label">Skipped</div></div>' +
        (pn > 0 ? '<div class="stat-card pending"><div class="value">' + pn + '</div><div class="label">Pending</div></div>' : '') +
        '<div class="stat-card stalled"><div class="value">' + stl + '</div><div class="label">Prob. Stale</div></div>' +
        '<div class="stat-card"><div class="value">' + rate + '%</div><div class="label">Success Rate</div></div>';
    applySortToFiltered();
    if (viewMode === 'grouped' && (currentTab === 'failures' || currentTab === 'skipped' || currentTab === 'pending' || currentTab === 'complete')) {{ renderGroupedView(); }}
    else {{ renderPage(); }}
}}

function renderGroupedView() {{
    const cid = currentTab + 'Content';
    if (!filteredData.length) {{
        document.getElementById(cid).innerHTML = '<div class="no-data"><p>No resources match filters</p></div>';
        document.getElementById('paginationContainer').classList.add('hidden'); return;
    }}
    const groups = {{}};
    filteredData.forEach(r => {{
        const pattern = getErrorPattern(r.error_message);
        if (!groups[pattern]) groups[pattern] = {{ count: 0, types: new Set(), orgs: new Set(), sample: r.error_message }};
        groups[pattern].count++;
        groups[pattern].types.add(r.resource_type);
        if (r.org_name) groups[pattern].orgs.add(r.org_name);
    }});
    const sorted = Object.entries(groups).sort((a, b) => b[1].count - a[1].count);
    let html = '<div class="resource-type-section"><h3>Error Patterns (' + sorted.length + ' patterns, ' + filteredData.length + ' resources)</h3>';
    html += '<table style="table-layout:auto"><thead><tr><th>Error Pattern</th><th>Count</th><th>Resource Types</th><th>Orgs Affected</th></tr></thead><tbody>';
    sorted.forEach(([pattern, data]) => {{
        const sample = data.sample || '';
        const searchVal = sample.substring(0, 60).replace(/"/g, '&quot;');
        html += '<tr class="group-row" onclick="applyPatternFilter(\\'' + searchVal.replace(/'/g, "\\\\'") + '\\')">' +
            '<td class="error-cell" style="max-width:500px;">' + escapeHtml(pattern) + '</td>' +
            '<td><span class="group-count">' + data.count + '</span></td>' +
            '<td style="font-size:0.85em;">' + Array.from(data.types).sort().join(', ') + '</td>' +
            '<td style="font-size:0.85em;">' + data.orgs.size + ' org' + (data.orgs.size !== 1 ? 's' : '') + '</td></tr>';
    }});
    html += '</tbody></table></div>';
    document.getElementById(cid).innerHTML = html;
    document.getElementById('paginationContainer').classList.add('hidden');
}}

function applyPatternFilter(text) {{
    viewMode = 'flat';
    document.getElementById('groupToggle').classList.remove('active');
    document.getElementById('searchInput').value = text;
    currentPage = 1; renderDetailTab();
}}

function renderPage() {{
    const start = (currentPage-1)*itemsPerPage, end = start+itemsPerPage, page = filteredData.slice(start,end), cid = currentTab + 'Content';
    if (page.length===0) {{ document.getElementById(cid).innerHTML = '<div class="no-data"><p>No resources match filters</p></div>'; document.getElementById('paginationContainer').classList.add('hidden'); return; }}
    page.forEach((r, idx) => {{ r._absIdx = start + idx; }});
    const byType = {{}}; page.forEach(r => {{ if (!byType[r.resource_type]) byType[r.resource_type]=[]; byType[r.resource_type].push(r); }});
    const showError = currentTab !== 'successful';
    const showOrg = !currentOrg;
    const sa = (col) => currentSort.column === col ? (currentSort.direction === 'asc' ? ' \\u25B2' : ' \\u25BC') : '';
    let html = ''; Object.keys(byType).sort().forEach(rt => {{
        const res = byType[rt];
        html += '<div class="resource-type-section"><h3>' + rt + ' (' + res.length + ')</h3><div style="overflow-x:auto"><table><thead><tr>' +
            '<th class="sortable" onclick="sortData(\\'source_id\\')" style="width:80px">Source ID' + sa('source_id') + '<div class="resizer"></div></th>' +
            '<th class="sortable" onclick="sortData(\\'source_name\\')" style="width:180px">Name' + sa('source_name') + '<div class="resizer"></div></th>' +
            (showOrg ? '<th class="sortable" onclick="sortData(\\'org_name\\')" style="width:140px">Organization' + sa('org_name') + '<div class="resizer"></div></th>' : '') +
            '<th class="sortable" onclick="sortData(\\'status\\')" style="width:100px">Migration Status' + sa('status') + '<div class="resizer"></div></th>' +
            (showError ? '<th style="width:160px">Error<div class="resizer"></div></th><th style="width:280px">Error Detail<div class="resizer"></div></th>' : '') +
            '<th class="sortable" onclick="sortData(\\'last_job_run\\')" style="width:110px">Last Run' + sa('last_job_run') + '<div class="resizer"></div></th>' +
            '<th class="sortable" onclick="sortData(\\'sync_status\\')" style="width:95px">Sync Status' + sa('sync_status') + '<div class="resizer"></div></th>' +
            '<th class="sortable" onclick="sortData(\\'modified\\')" style="width:100px">Modified' + sa('modified') + '<div class="resizer"></div></th>' +
            '<th class="sortable" onclick="sortData(\\'resource_status\\')" style="width:100px">AAP Status' + sa('resource_status') + '<div class="resizer"></div></th>' +
            '<th style="width:180px">Ownership<div class="resizer"></div></th>' +
            '</tr></thead><tbody>';
        res.forEach((r,i) => {{
            const sc = {{'completed':'status-completed','failed':'status-failed','skipped':'status-skipped','pending':'status-pending'}}[r.status];
            const err = r.error_message||(r.status==='completed'?'Success':'No message');
            const te = err.length>100?err.substring(0,100)+'...':err;
            const md = r.modified?r.modified.split('T')[0]:'N/A';
            const rsc = r.resource_status==='Active'?'resource-status-active':r.resource_status==='Probably Stale'?'resource-status-stale':'resource-status-unknown';
            const lastRun = formatLastRun(r);
            const syncSt = formatSyncStatus(r);
            const ownerCreated = r.created_by && r.created_by !== 'N/A' ? r.created_by : '';
            const ownerModified = r.modified_by && r.modified_by !== 'N/A' ? r.modified_by : '';
            let ownerHtml = '';
            if (ownerCreated) ownerHtml += '<div style="font-size:0.8em;color:#6c757d;">Created: ' + escapeHtml(ownerCreated) + '</div>';
            if (ownerModified && ownerModified !== ownerCreated) ownerHtml += '<div style="font-size:0.8em;color:#495057;">Edited: ' + escapeHtml(ownerModified) + '</div>';
            else if (ownerModified) ownerHtml += '<div style="font-size:0.8em;color:#495057;">Edited: ' + escapeHtml(ownerModified) + '</div>';
            if (!ownerHtml) ownerHtml = 'N/A';
            html += '<tr style="height:' + tableRowHeight + 'px;font-size:' + tableFontSize + 'px">' +
                '<td>' + r.source_id + '</td>' +
                '<td class="name-link" onclick="showErrorDetail(' + r._absIdx + ')" title="' + escapeHtml(r.source_name||'') + '">' + escapeHtml(r.source_name||'N/A') + '</td>' +
                (showOrg ? '<td title="' + escapeHtml(r.org_name||'') + '">' + escapeHtml(r.org_name||'N/A') + '</td>' : '') +
                '<td><span class="' + sc + '">' + r.status + '</span></td>' +
                (showError ? '<td><span class="error-key-badge" title="' + escapeHtml(r.error_key||'') + '">' + escapeHtml(r.error_key||'') + '</span></td><td class="error-cell" onclick="showErrorDetail(' + r._absIdx + ')">' + escapeHtml(te) + '</td>' : '') +
                '<td>' + lastRun + '</td>' +
                '<td>' + syncSt + '</td>' +
                '<td class="modified-cell">' + md + '</td>' +
                '<td><span class="' + rsc + '">' + (r.resource_status||'Unknown') + '</span></td>' +
                '<td style="white-space:normal;">' + ownerHtml + '</td></tr>';
        }}); html += '</tbody></table></div></div>';
    }}); document.getElementById(cid).innerHTML = html; initResizers();
    const tp = Math.ceil(filteredData.length/itemsPerPage);
    if (tp>1) {{ document.getElementById('paginationContainer').classList.remove('hidden'); document.getElementById('pageInfo').textContent = 'Page ' + currentPage + ' of ' + tp + ' (' + (start+1) + '-' + Math.min(end,filteredData.length) + ' of ' + filteredData.length + ')'; document.getElementById('prevPage').disabled = currentPage===1; document.getElementById('nextPage').disabled = currentPage===tp; }}
    else document.getElementById('paginationContainer').classList.add('hidden');
}}

function initResizers() {{ document.querySelectorAll('.resizer').forEach(rs => {{ let sx,sw,th; rs.addEventListener('mousedown',e => {{ th=rs.parentElement; sx=e.pageX; sw=th.offsetWidth; rs.classList.add('active'); document.addEventListener('mousemove',mm); document.addEventListener('mouseup',mu); e.preventDefault(); }}); function mm(e){{ const nw=sw+(e.pageX-sx); if(nw>40) th.style.width=nw+'px'; }} function mu(){{ rs.classList.remove('active'); document.removeEventListener('mousemove',mm); document.removeEventListener('mouseup',mu); }} }}); }}

function showErrorDetail(idx) {{
    const r = filteredData[idx]; if (!r) return;
    document.getElementById('modalTitle').textContent = (r.source_name||'N/A') + ' (ID: ' + r.source_id + ')';
    let b = '';
    b += mf('Organization', r.org_name||'N/A');
    b += mf('Resource Type', r.resource_type);
    b += mf('Source ID', r.source_id);
    b += mf('Name', r.source_name||'N/A');
    b += mf('Migration Status', r.status);
    b += mf('Error Classification', r.error_key||'N/A');
    if (r.error_explanation) {{
        b += '<div class="modal-field"><div class="field-label">Error Explanation</div><div class="field-value explanation-box">' + escapeHtml(r.error_explanation) + '</div></div>';
    }}
    b += mf('Full Error Message', r.error_message||'No error message', true);
    b += mf('Last Modified', r.modified?r.modified.replace('T',' ').replace('Z',''):'N/A');
    b += mf('Resource Status in AAP', r.resource_status||'Unknown');
    b += mf('Created By', r.created_by||'N/A');
    b += mf('Last Modified By', r.modified_by||'N/A');
    b += mf('Created', r.created?r.created.replace('T',' ').replace('Z',''):'N/A');
    b += mf('Last Job Run', r.last_job_run?r.last_job_run.replace('T',' ').replace('Z',''):'N/A');
    b += mf('Last Job Failed', r.last_job_failed != null ? String(r.last_job_failed) : 'N/A');
    b += mf('Sync Status', r.sync_status||'N/A');
    b += mf('Next Job Run', r.next_job_run?r.next_job_run.replace('T',' ').replace('Z',''):'N/A');
    b += mf('Phase', r.phase||'N/A');
    document.getElementById('modalBody').innerHTML = b;
    document.getElementById('errorModal').classList.add('visible');
}}
function mf(l,v,isErr) {{ return '<div class="modal-field"><div class="field-label">' + l + '</div><div class="field-value' + (isErr?' error-text':'') + '">' + escapeHtml(String(v)) + '</div></div>'; }}
function closeModal() {{ document.getElementById('errorModal').classList.remove('visible'); }}

function exportCSV() {{
    if (filteredData.length===0) return;
    const cols = ['org_name','resource_type','source_id','source_name','status','error_key','error_explanation','error_message','resource_status','modified','created_by','modified_by','created','last_job_run','last_job_failed','next_job_run','sync_status'];
    const headers = ['Organization','Resource Type','Source ID','Name','Migration Status','Error Classification','Error Explanation','Error Message','Resource Status in AAP','Last Modified','Created By','Last Modified By','Created','Last Job Run','Last Job Failed','Next Job Run','Sync Status'];
    let csv = headers.join(',') + '\\n';
    filteredData.forEach(r => {{
        csv += cols.map(c => {{
            let v = r[c] != null ? String(r[c]) : '';
            if (v.includes(',') || v.includes('"') || v.includes('\\n')) v = '"' + v.replace(/"/g, '""') + '"';
            return v;
        }}).join(',') + '\\n';
    }});
    const blob = new Blob([csv],{{type:'text/csv'}}), url = URL.createObjectURL(blob), a = document.createElement('a');
    a.href = url; a.download = 'report_' + (currentOrg||'all') + '_' + new Date().toISOString().split('T')[0] + '.csv'; a.click(); URL.revokeObjectURL(url);
}}

function exportCSVWithEmails() {{
    if (filteredData.length===0) return;
    const cols = ['org_name','resource_type','source_id','source_name','status','error_key','error_explanation','error_message','resource_status','modified','created_by','modified_by','created','last_job_run','last_job_failed','next_job_run','sync_status'];
    const headers = ['Organization','Resource Type','Source ID','Name','Migration Status','Error Classification','Error Explanation','Error Message','Resource Status in AAP','Last Modified','Created By','Last Modified By','Created','Last Job Run','Last Job Failed','Next Job Run','Sync Status','Emails'];
    let csv = headers.join(',') + '\\n';
    filteredData.forEach(r => {{
        const row = cols.map(c => {{
            let v = r[c] != null ? String(r[c]) : '';
            if (v.includes(',') || v.includes('"') || v.includes('\\n')) v = '"' + v.replace(/"/g, '""') + '"';
            return v;
        }});
        const orgEmails = (DATA.user_emails[r.org_name] || []).join('; ');
        row.push('"' + orgEmails.replace(/"/g, '""') + '"');
        csv += row.join(',') + '\\n';
    }});
    const blob = new Blob([csv],{{type:'text/csv'}}), url = URL.createObjectURL(blob), a = document.createElement('a');
    a.href = url; a.download = 'report_emails_' + (currentOrg||'all') + '_' + new Date().toISOString().split('T')[0] + '.csv'; a.click(); URL.revokeObjectURL(url);
}}

function attachEventListeners() {{
    document.querySelectorAll('.tab').forEach(t => t.addEventListener('click',()=>switchTab(t.dataset.tab)));
    document.getElementById('orgSelect').addEventListener('change',() => {{
        currentOrg = document.getElementById('orgSelect').value; currentPage=1;
        populateResourceTypeFilter(); populateErrorFilter();
        if (currentTab === 'summary') {{ renderSummaryTab(); }}
        else {{ renderDetailTab(); }}
    }});
    document.getElementById('resourceTypeFilter').addEventListener('change',()=>{{ currentPage=1; populateErrorFilter(); renderDetailTab(); }});
    document.getElementById('errorFilter').addEventListener('change',()=>{{ currentPage=1; renderDetailTab(); }});
    document.getElementById('migrationStatusFilter').addEventListener('change',()=>{{ currentPage=1; renderDetailTab(); }});
    document.getElementById('statusFilter').addEventListener('change',()=>{{ currentPage=1; renderDetailTab(); }});
    document.getElementById('searchInput').addEventListener('input',()=>{{
        clearTimeout(_searchDebounceTimer);
        _searchDebounceTimer = setTimeout(() => {{ currentPage=1; renderDetailTab(); }}, 200);
    }});
    document.getElementById('summarySearchInput').addEventListener('input',()=>{{
        clearTimeout(_searchDebounceTimer);
        _searchDebounceTimer = setTimeout(() => {{ renderSummaryTab(); }}, 200);
    }});
    document.getElementById('prevPage').addEventListener('click',()=>{{ if(currentPage>1){{ currentPage--; renderPage(); }} }});
    document.getElementById('nextPage').addEventListener('click',()=>{{ const tp=Math.ceil(filteredData.length/itemsPerPage); if(currentPage<tp){{ currentPage++; renderPage(); }} }});
    document.getElementById('rowHeightSlider').addEventListener('input',e=>{{ tableRowHeight=parseInt(e.target.value); document.getElementById('rowHeightValue').textContent=tableRowHeight+'px'; renderPage(); }});
    document.getElementById('fontSizeSlider').addEventListener('input',e=>{{ tableFontSize=parseInt(e.target.value); document.getElementById('fontSizeValue').textContent=tableFontSize+'px'; renderPage(); }});
    document.getElementById('groupToggle').addEventListener('click', () => {{
        viewMode = viewMode === 'flat' ? 'grouped' : 'flat';
        document.getElementById('groupToggle').classList.toggle('active', viewMode === 'grouped');
        currentPage = 1; renderDetailTab();
    }});
    document.getElementById('errorModal').addEventListener('click',e=>{{ if(e.target===document.getElementById('errorModal')) closeModal(); }});
    document.addEventListener('keydown',e=>{{ if(e.key==='Escape') closeModal(); }});
}}

function escapeHtml(t) {{ const d=document.createElement('div'); d.textContent=t; return d.innerHTML; }}
init();
</script>
</body>
</html>"""

    return html
