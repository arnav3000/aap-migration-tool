"""
Banner and attribution module for AAP Migration Tool.
Provides consistent branding across CLI, TUI, container, and HTML reports.

Created by: Arnav Bhati
"""

import datetime

# Version info
TOOL_NAME = "AAP Migration Tool"
CREATOR = "Arnav Bhati"
ORGANIZATION = "Red Hat"


def get_version():
    """Get tool version from package metadata."""
    try:
        from importlib.metadata import version
        return version("aap-migration")
    except Exception:
        return "0.5.x"  # Fallback


# ============================================================================
# CLI/TUI BANNERS
# ============================================================================

def get_cli_banner():
    """
    Get ASCII banner for CLI/TUI usage.
    Returns multi-line string.
    """
    ver = get_version()
    banner = f"""
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║              AAP Migration Tool v{ver:<10}                   ║
║              Created by: Arnav Bhati                          ║
║              Red Hat - Ansible Automation Platform            ║
║                                                               ║
╚═══════════════════════════════════════════════════════════════╝"""
    return banner.strip()


def get_cli_banner_simple():
    """
    Get simple one-line banner for CLI.
    """
    ver = get_version()
    return f"AAP Migration Tool v{ver} | Created by Arnav Bhati (Red Hat)"


def get_container_motd():
    """
    Get Message of the Day for container login.
    """
    ver = get_version()
    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  AAP Migration Tool v{ver}
  Created by: Arnav Bhati
  Organization: Red Hat

  For help: aap-bridge --help
  Documentation: See README.md
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


# ============================================================================
# HTML REPORT ATTRIBUTION
# ============================================================================

def get_html_meta_tags():
    """
    Get HTML meta tags for report attribution.
    """
    ver = get_version()
    return f'''
    <meta name="author" content="{CREATOR}">
    <meta name="creator" content="{TOOL_NAME} by {CREATOR}">
    <meta name="generator" content="aap-bridge v{ver} - {CREATOR} ({ORGANIZATION})">
    <meta name="copyright" content="Copyright 2024-2026 {CREATOR}">
'''


def get_html_hidden_comments():
    """
    Get list of hidden HTML comments for attribution.
    Returns list of comment strings to scatter throughout HTML.
    """
    return [
        f"<!-- {TOOL_NAME} - Created by {CREATOR} -->",
        f"<!-- Report Generator: {CREATOR} ({ORGANIZATION}) -->",
        f"<!-- aap-bridge-creator:{CREATOR.lower().replace(' ', '.')} -->",
        f"<!-- Copyright 2024-2026 {CREATOR} - {ORGANIZATION} -->",
    ]


def get_html_footer():
    """
    Get visible HTML footer with attribution.
    """
    ver = get_version()
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f'''
<footer class="creator-attribution" style="margin-top: 2em; padding: 1.5em; border-top: 3px solid #e74c3c; text-align: center; background: #f8f9fa;">
    <hr>
    <p style="margin: 0.5em 0;">
        <strong>{TOOL_NAME}</strong> |
        Created by <strong>{CREATOR}</strong> |
        {ORGANIZATION} - Ansible Automation Platform
    </p>
    <p style="font-size: 0.9em; color: #666; margin: 0.5em 0;">
        Report generated: {timestamp} |
        Tool version: {ver}
    </p>
</footer>
'''


def get_html_css_watermark():
    """
    Get CSS for always-visible watermark in bottom-right corner.
    """
    return f'''
body::after {{
    content: "{TOOL_NAME} | {CREATOR}";
    position: fixed;
    bottom: 10px;
    right: 10px;
    font-size: 10px;
    color: #999;
    pointer-events: none;
    opacity: 0.6;
    z-index: 9999;
}}
'''


def get_html_js_protection():
    """
    Get minified JavaScript that re-injects attribution if removed.
    """
    return (
        '<script>\n'
        '(function(){var c="' + CREATOR + '",t="' + TOOL_NAME + '";'
        'window.addEventListener("load",function(){'
        'if(!document.querySelector(".creator-attribution")){'
        'var e=document.createElement("div");'
        'e.className="creator-attribution";'
        'e.innerHTML=\'<hr><p><strong>\'+t+\'</strong> | '
        'Created by <strong>\'+c+\'</strong></p>\';'
        'e.style.cssText="margin-top:2em;padding:1em;'
        'border-top:2px solid #ccc;text-align:center;";'
        'document.body.appendChild(e)}})})();\n'
        '</script>'
    )


def inject_html_attribution(html_content):
    """
    Inject all attribution layers into HTML content.

    Args:
        html_content (str): Original HTML content

    Returns:
        str: HTML with attribution injected
    """
    # Find insertion points
    head_end = html_content.find('</head>')
    body_start = html_content.find('<body>')
    body_end = html_content.find('</body>')

    if head_end == -1 or body_start == -1 or body_end == -1:
        # Malformed HTML, return as-is
        return html_content

    # Layer 1: Meta tags in head
    html_content = (
        html_content[:head_end]
        + get_html_meta_tags()
        + html_content[head_end:]
    )

    # Recalculate positions after insertion
    body_start = html_content.find('<body>')
    body_end = html_content.find('</body>')

    # Layer 2: Hidden comment after <body>
    comments = get_html_hidden_comments()
    html_content = (
        html_content[:body_start + 6]
        + '\n' + comments[0]
        + html_content[body_start + 6:]
    )

    # Recalculate
    body_end = html_content.find('</body>')

    # Layer 3: CSS watermark in style tag (before </head>)
    head_end = html_content.find('</head>')
    style_tag = f'\n<style>\n{get_html_css_watermark()}</style>\n'
    html_content = (
        html_content[:head_end]
        + style_tag
        + html_content[head_end:]
    )

    # Recalculate
    body_end = html_content.find('</body>')

    # Layer 4: Visible footer before </body>
    html_content = (
        html_content[:body_end]
        + '\n' + comments[1] + '\n'
        + get_html_footer()
        + html_content[body_end:]
    )

    # Recalculate
    body_end = html_content.find('</body>')

    # Layer 5: JS protection before </body>
    html_content = (
        html_content[:body_end]
        + '\n' + comments[2] + '\n'
        + get_html_js_protection()
        + html_content[body_end:]
    )

    # Recalculate
    body_end = html_content.find('</body>')

    # Layer 6: Final hidden comment before </body>
    html_content = (
        html_content[:body_end]
        + '\n' + comments[3] + '\n'
        + html_content[body_end:]
    )

    return html_content


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def print_cli_banner():
    """Print CLI banner to stdout."""
    print(get_cli_banner())


def print_simple_banner():
    """Print simple one-line banner to stdout."""
    print(get_cli_banner_simple())
