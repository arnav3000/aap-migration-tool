"""
Banner and attribution module for AAP Migration Toolkit.

Provides consistent branding across container MOTD and HTML reports.
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


def get_version() -> str:
    """Get tool version from package metadata."""
    try:
        from importlib.metadata import version

        return version("aap-bridge")
    except Exception:
        return "dev"


def get_container_motd() -> str:
    """Get Message of the Day for container shell login."""
    ver = get_version()
    return f"""
{RED}{BOLD}  AAP Migration Toolkit{R} {DIM}v{ver}{R}
  {DIM}Ansible Automation Platform 2.4 -> 2.6{R}

  {CYAN}{BOLD}Web UI:{R}
  {GREEN}${R} Open the AAP Bridge web interface (engine + ui containers)

  {CYAN}{BOLD}API:{R}
  {GREEN}${R} aap-bridge {WHITE}{BOLD}--host 0.0.0.0 --port 8000{R}

  {CYAN}{BOLD}Paths:{R}
  {WHITE}Config:{R}   /app/aap-bridge/config/config.yaml
  {WHITE}Exports:{R}  /app/aap-bridge/exports/
  {WHITE}Reports:{R}  /app/aap-bridge/logs/

  {DIM}Crafted by{R} {WHITE}{BOLD}{CREATORS}{R} {DIM}|{R} {RED}{BOLD}{ORGANIZATION}{R}
"""


def get_html_meta_tags() -> str:
    """Get HTML meta tags for report attribution."""
    ver = get_version()
    return f"""
    <meta name="author" content="{CREATORS} ({GH_HANDLE})">
    <meta name="generator" content="{TOOL_NAME} v{ver}">
"""


def get_html_footer() -> str:
    """Get visible HTML footer with attribution."""
    ver = get_version()
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""
<footer style="margin-top: 2em; padding: 1em; border-top: 2px solid #cc0000; text-align: center; color: #666; font-size: 0.9em;">
    <p><strong>{TOOL_NAME}</strong> v{ver} | Crafted by {CREATORS} | {ORGANIZATION}</p>
    <p><a href="https://github.com/arnav3000" style="color: #0366d6; text-decoration: none;">{GH_HANDLE}</a></p>
    <p>Report generated: {timestamp}</p>
</footer>
"""


def inject_html_attribution(html_content: str) -> str:
    """Inject attribution into HTML report content."""
    head_end = html_content.find("</head>")
    body_end = html_content.find("</body>")

    if head_end == -1 or body_end == -1:
        return html_content

    html_content = html_content[:head_end] + get_html_meta_tags() + html_content[head_end:]

    body_end = html_content.find("</body>")
    html_content = html_content[:body_end] + get_html_footer() + html_content[body_end:]

    return html_content
