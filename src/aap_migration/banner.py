"""
Banner and attribution module for AAP Migration Toolkit.

Provides consistent branding across CLI, container, and HTML reports.
"""

import datetime

TOOL_NAME = "AAP Migration Toolkit"
CREATORS = "Ansible Automation Enthusiasts"
ORGANIZATION = "Red Hat"
GH_HANDLE = "@arnav3000"

R = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
WHITE = "\033[37m"


def get_version():
    """Get tool version from package metadata."""
    try:
        from importlib.metadata import version
        return version("aap-bridge")
    except Exception:
        return "dev"


def get_cli_banner():
    """Get informative banner for CLI startup."""
    ver = get_version()
    line = "=" * 62
    return f"""
{CYAN}{line}{R}

  {RED}{BOLD}AAP Migration Toolkit{R} {DIM}v{ver}{R}
  Automated migration from {YELLOW}{BOLD}AAP 2.4{R} to {GREEN}{BOLD}AAP 2.6{R}

{CYAN}{line}{R}

{CYAN}{BOLD}  Resources:{R}
  {YELLOW}Identity       {R} Organizations, Teams, Users, Credentials
  {YELLOW}Automation     {R} Job Templates, Workflow Templates, Projects
  {YELLOW}Infrastructure {R} Inventories, Hosts, Groups, Sources
  {YELLOW}Operations     {R} Schedules, Notifications, Execution Environments

{CYAN}{BOLD}  Migration Phases (run in order):{R}

  {WHITE}{BOLD}# Phase 1: Foundation{R}
  {GREEN}${R} aap-bridge migrate -r organizations --skip-prep
  {GREEN}${R} aap-bridge migrate -r users --skip-prep
  {GREEN}${R} aap-bridge migrate -r teams --skip-prep

  {DIM}NOTE: The above migrates ONLY local users/teams.{R}
  {DIM}For LDAP/AD users: migrate settings first (Phase 9),{R}
  {DIM}skip users/teams - they authenticate automatically.{R}

  {WHITE}{BOLD}# Phase 2: Credentials (must be 100% complete){R}
  {GREEN}${R} aap-bridge migrate -r credential_types --skip-prep
  {GREEN}${R} aap-bridge migrate -r credentials --skip-prep

  {WHITE}{BOLD}# Phase 3: Infrastructure{R}
  {GREEN}${R} aap-bridge migrate -r execution_environments --skip-prep
  {GREEN}${R} aap-bridge migrate -r projects --skip-prep
  {GREEN}${R} aap-bridge migrate -r inventories --skip-prep
  {GREEN}${R} aap-bridge migrate -r inventory_sources --skip-prep

  {WHITE}{BOLD}# Phase 4: Hosts{R}
  {GREEN}${R} aap-bridge migrate -r hosts --skip-prep

  {WHITE}{BOLD}# Phase 5: Instance Groups{R}
  {GREEN}${R} aap-bridge migrate -r instance_groups --skip-prep

  {WHITE}{BOLD}# Phase 6: Automation{R}
  {GREEN}${R} aap-bridge migrate -r job_templates --skip-prep
  {GREEN}${R} aap-bridge migrate -r workflow_job_templates --skip-prep

  {WHITE}{BOLD}# Phase 7: Applications (OAuth){R}
  {GREEN}${R} aap-bridge migrate -r applications --skip-prep

  {WHITE}{BOLD}# Phase 8: Schedules{R}
  {GREEN}${R} aap-bridge migrate -r schedules --skip-prep

  {WHITE}{BOLD}# Phase 9: Settings (optional - review before applying){R}
  {GREEN}${R} aap-bridge migrate -r settings --skip-prep

  {WHITE}{BOLD}# Phase 10: Notification Templates (optional){R}
  {GREEN}${R} aap-bridge migrate -r notification_templates --skip-prep

  {RED}{BOLD}IMPORTANT:{R} Phase ordering matters. Each phase depends on
  resources from prior phases. Run them sequentially.

  {GREEN}${R} aap-bridge {WHITE}{BOLD}migration-report{R}  {DIM}Generate report after migration{R}
  {GREEN}${R} aap-bridge {WHITE}{BOLD}--help{R}             {DIM}Show all commands{R}

{CYAN}{line}{R}
  {DIM}Crafted by{R} {WHITE}{BOLD}{CREATORS}{R} {DIM}|{R} {RED}{BOLD}{ORGANIZATION}{R}
"""


def get_container_motd():
    """Get Message of the Day for container shell login."""
    ver = get_version()
    return f"""
{RED}{BOLD}  AAP Migration Toolkit{R} {DIM}v{ver}{R}
  {DIM}Ansible Automation Platform 2.4 -> 2.6{R}

  {CYAN}{BOLD}Quick Start:{R}
  {GREEN}${R} aap-bridge {WHITE}{BOLD}migrate -r organizations --skip-prep{R}
  {GREEN}${R} aap-bridge {WHITE}{BOLD}migration-report{R}
  {GREEN}${R} aap-bridge {WHITE}{BOLD}--help{R}

  {CYAN}{BOLD}Paths:{R}
  {WHITE}Config:{R}   /app/aap-bridge/config/config.yaml
  {WHITE}Exports:{R}  /app/aap-bridge/exports/
  {WHITE}Reports:{R}  /app/aap-bridge/logs/

  {DIM}Crafted by{R} {WHITE}{BOLD}{CREATORS}{R} {DIM}|{R} {RED}{BOLD}{ORGANIZATION}{R}
"""


def get_html_meta_tags():
    """Get HTML meta tags for report attribution."""
    ver = get_version()
    return f'''
    <meta name="author" content="{CREATORS} ({GH_HANDLE})">
    <meta name="generator" content="{TOOL_NAME} v{ver}">
'''


def get_html_footer():
    """Get visible HTML footer with attribution."""
    ver = get_version()
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f'''
<footer style="margin-top: 2em; padding: 1em; border-top: 2px solid #cc0000; text-align: center; color: #666; font-size: 0.9em;">
    <p><strong>{TOOL_NAME}</strong> v{ver} | Crafted by {CREATORS} | {ORGANIZATION}</p>
    <p><a href="https://github.com/arnav3000" style="color: #0366d6; text-decoration: none;">{GH_HANDLE}</a></p>
    <p>Report generated: {timestamp}</p>
</footer>
'''


def inject_html_attribution(html_content):
    """Inject attribution into HTML report content."""
    head_end = html_content.find('</head>')
    body_end = html_content.find('</body>')

    if head_end == -1 or body_end == -1:
        return html_content

    html_content = (
        html_content[:head_end]
        + get_html_meta_tags()
        + html_content[head_end:]
    )

    body_end = html_content.find('</body>')
    html_content = (
        html_content[:body_end]
        + get_html_footer()
        + html_content[body_end:]
    )

    return html_content


def print_cli_banner():
    """Print CLI banner to stdout."""
    print(get_cli_banner())
