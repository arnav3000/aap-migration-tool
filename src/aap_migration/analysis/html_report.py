"""Self-contained HTML report generator for dependency analysis.

Dependencies are presented strictly as nested lists — no SVG mind map,
no node graph. All CSS and JavaScript are embedded inline for
air-gapped environments. No external dependencies, CDNs, or internet
connection required.
"""

from __future__ import annotations

import html
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aap_migration.analysis.dependency_analyzer import GlobalDependencyReport


# Friendly display names for AAP resource types
_RESOURCE_TYPE_DISPLAY = {
    "job_templates": "Job Templates",
    "workflow_job_templates": "Workflow Templates",
    "projects": "Projects",
    "inventories": "Inventories",
    "inventory_sources": "Inventory Sources",
    "credentials": "Credentials",
    "teams": "Teams",
    "schedules": "Schedules",
    "notification_templates": "Notifications",
    "hosts": "Hosts",
    "inventory_groups": "Groups",
    "credential_input_sources": "Credential Inputs",
}


def _display_type(rtype: str) -> str:
    """Friendly label for a resource type."""
    return _RESOURCE_TYPE_DISPLAY.get(rtype, rtype.replace("_", " ").title())


def generate_html_report(report: GlobalDependencyReport) -> str:
    """Generate self-contained HTML report with list-based dependency display.

    Args:
        report: Global dependency analysis report

    Returns:
        Complete HTML string with embedded CSS/JS
    """
    # Build per-org data the JS side needs to render the lists.
    orgs_data = []
    for org_name, org_report in report.org_reports.items():
        # Resource type summary: just type -> count, no individual items
        # (individual resources are not part of the list-based dependency view).
        resource_summary = []
        for rtype, items in org_report.resources.items():
            if not items:
                continue
            resource_summary.append({
                "type": rtype,
                "display": _display_type(rtype),
                "count": len(items),
            })
        resource_summary.sort(key=lambda x: x["display"])

        # Dependencies grouped by source org, each dependency a resource with
        # the list of local resources that require it.
        dependencies = []
        for dep_org, deps in sorted(org_report.dependencies.items()):
            resources = []
            for dep in deps:
                resources.append({
                    "type": dep.resource_type,
                    "type_display": _display_type(dep.resource_type),
                    "name": dep.resource_name,
                    "id": dep.resource_id,
                    "used_by": [
                        {
                            "type": _display_type(u["type"]),
                            "name": u["name"],
                            "id": u["id"],
                        }
                        for u in dep.required_by
                    ],
                })
            # Sort: by type then name for stable list order
            resources.sort(key=lambda r: (r["type_display"], r["name"].lower()))
            dependencies.append({
                "org": dep_org,
                "resource_count": len(resources),
                "resources": resources,
            })

        orgs_data.append({
            "name": org_name,
            "id": org_report.org_id,
            "total_resources": org_report.resource_count,
            "has_dependencies": org_report.has_cross_org_deps,
            "required_before": org_report.required_migrations_before,
            "resource_summary": resource_summary,
            "dependencies": dependencies,
        })

    # Phases
    phases_data = []
    for phase in report.migration_phases:
        phases_data.append({
            "phase": phase["phase"],
            "description": phase["description"],
            "orgs": phase["orgs"],
            "has_cycle": phase.get("has_cycle", False),
            "cycles": phase.get("cycles", []),
        })

    # Cycles (top-level)
    cycles_data = list(getattr(report, "cycles", []) or [])

    # Serialize to JSON for safe inline embedding
    orgs_json = json.dumps(orgs_data, indent=2)
    phases_json = json.dumps(phases_data, indent=2)
    cycles_json = json.dumps(cycles_data, indent=2)

    # Header summary numbers
    total_orgs = report.total_organizations
    indep_count = len(report.independent_orgs)
    dep_count = len(report.dependent_orgs)
    phase_count = len(report.migration_phases)
    cycle_count = len(cycles_data)
    analysis_date = report.analysis_date.strftime("%Y-%m-%d %H:%M:%S")
    source_url = html.escape(str(report.source_url))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AAP Migration - Dependency Analysis</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f8fafc;
            color: #1e293b;
            line-height: 1.5;
        }}

        .container {{
            display: grid;
            grid-template-rows: auto auto 1fr;
            min-height: 100vh;
        }}

        .header {{
            background: linear-gradient(135deg, #1e3a8a 0%, #7c3aed 100%);
            color: white;
            padding: 24px 40px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.15);
        }}

        .header h1 {{
            font-size: 1.7em;
            margin-bottom: 4px;
        }}

        .header .subtitle {{
            font-size: 0.85em;
            opacity: 0.85;
        }}

        .header-controls {{
            display: flex;
            gap: 24px;
            align-items: center;
            margin-top: 16px;
            flex-wrap: wrap;
        }}

        .search-box {{
            flex: 1;
            min-width: 240px;
            max-width: 400px;
        }}

        .search-input {{
            width: 100%;
            padding: 10px 14px;
            border: none;
            border-radius: 6px;
            font-size: 0.9em;
            background: rgba(255,255,255,0.95);
            color: #1e293b;
        }}

        .stats-bar {{
            display: flex;
            gap: 24px;
            font-size: 0.85em;
            flex-wrap: wrap;
        }}

        .stat-item {{ display: flex; align-items: center; gap: 6px; }}

        .tabs {{
            display: flex;
            background: #f1f5f9;
            border-bottom: 2px solid #cbd5e1;
            padding: 0 40px;
        }}

        .tab {{
            padding: 14px 26px;
            cursor: pointer;
            border: none;
            background: none;
            font-size: 0.95em;
            font-weight: 600;
            color: #64748b;
            border-bottom: 3px solid transparent;
            transition: color 0.15s, border-color 0.15s, background 0.15s;
        }}

        .tab:hover {{ background: rgba(59, 130, 246, 0.08); color: #3b82f6; }}
        .tab.active {{ color: #3b82f6; border-bottom-color: #3b82f6; background: white; }}

        .main-content {{ background: #f8fafc; }}
        .tab-content {{ display: none; padding: 32px 40px; }}
        .tab-content.active {{ display: block; }}

        h2 {{ color: #1e293b; margin-bottom: 16px; font-size: 1.4em; }}
        h3 {{ color: #1e293b; margin-bottom: 12px; font-size: 1.15em; }}

        .muted {{ color: #64748b; font-size: 0.9em; }}

        /* Stat cards */
        .stat-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-bottom: 28px;
        }}

        .stat-card {{
            background: white;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            text-align: center;
        }}

        .stat-value {{ font-size: 2.4em; font-weight: 700; color: #3b82f6; line-height: 1.1; }}
        .stat-label {{ font-size: 0.85em; color: #64748b; margin-top: 6px; }}
        .stat-card.success .stat-value {{ color: #10b981; }}
        .stat-card.warning .stat-value {{ color: #f59e0b; }}
        .stat-card.danger .stat-value {{ color: #ef4444; }}

        /* Generic card */
        .card {{
            background: white;
            border-radius: 10px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            padding: 24px;
            margin-bottom: 20px;
        }}

        /* Org summary lists (overview tab) */
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 20px;
        }}

        .org-list {{ list-style: none; }}

        .org-list li {{
            padding: 8px 12px;
            border-left: 3px solid #cbd5e1;
            background: #f8fafc;
            margin-bottom: 6px;
            border-radius: 4px;
            cursor: pointer;
            transition: background 0.15s, border-color 0.15s;
        }}

        .org-list li.independent {{ border-left-color: #10b981; }}
        .org-list li.dependent {{ border-left-color: #f59e0b; }}
        .org-list li:hover {{ background: #eff6ff; border-left-color: #3b82f6; }}

        .org-list .org-name {{ font-weight: 600; color: #1e293b; }}
        .org-list .org-meta {{ font-size: 0.8em; color: #64748b; margin-top: 2px; }}

        /* Cycles section */
        .cycles-card {{
            background: #fef2f2;
            border-left: 4px solid #ef4444;
            border-radius: 10px;
            padding: 20px 24px;
            margin-bottom: 24px;
        }}

        .cycles-card h3 {{ color: #b91c1c; }}

        .cycle-item {{
            background: white;
            padding: 12px 16px;
            border-radius: 6px;
            margin-top: 10px;
            border: 1px solid #fecaca;
        }}

        .cycle-item .cycle-label {{
            font-size: 0.8em;
            color: #991b1b;
            font-weight: 700;
            text-transform: uppercase;
            margin-bottom: 6px;
        }}

        .cycle-orgs {{ list-style: none; display: flex; flex-wrap: wrap; gap: 8px; }}

        .cycle-orgs li {{
            background: #fee2e2;
            color: #991b1b;
            padding: 4px 12px;
            border-radius: 4px;
            font-weight: 600;
            font-size: 0.9em;
            cursor: pointer;
        }}

        .cycle-orgs li:hover {{ background: #fecaca; }}

        /* Phase cards */
        .phases-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 20px;
        }}

        .phase-card {{
            background: white;
            border-radius: 10px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            overflow: hidden;
        }}

        .phase-header {{
            background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%);
            color: white;
            padding: 16px 20px;
            font-weight: 700;
            font-size: 1.1em;
        }}

        .phase-card.has-cycle .phase-header {{
            background: linear-gradient(135deg, #ef4444 0%, #b91c1c 100%);
        }}

        .phase-description {{
            padding: 12px 20px;
            background: #eff6ff;
            color: #1e40af;
            font-size: 0.85em;
        }}

        .phase-card.has-cycle .phase-description {{
            background: #fef2f2;
            color: #991b1b;
        }}

        .phase-orgs {{ padding: 16px 20px; list-style: none; }}

        .phase-orgs li {{
            padding: 10px 12px;
            margin-bottom: 6px;
            background: #f8fafc;
            border-left: 3px solid #3b82f6;
            border-radius: 4px;
            cursor: pointer;
            transition: background 0.15s, transform 0.15s;
        }}

        .phase-orgs li:hover {{ background: #eff6ff; transform: translateX(3px); }}
        .phase-orgs li.has-deps {{ border-left-color: #f59e0b; }}
        .phase-orgs li.in-cycle {{ border-left-color: #ef4444; }}

        .phase-orgs .org-name {{ font-weight: 600; }}
        .phase-orgs .org-meta {{ font-size: 0.8em; color: #64748b; margin-top: 2px; }}

        /* Organization detail */
        .org-selector {{
            background: white;
            padding: 20px 24px;
            border-radius: 10px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            margin-bottom: 20px;
        }}

        .org-dropdown {{ position: relative; max-width: 480px; }}

        .org-dropdown-btn {{
            width: 100%;
            padding: 12px 16px;
            background: white;
            border: 1px solid #cbd5e1;
            border-radius: 6px;
            font-size: 0.95em;
            cursor: pointer;
            text-align: left;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .org-dropdown-btn:hover {{ border-color: #3b82f6; }}

        .org-dropdown-menu {{
            position: absolute;
            top: calc(100% + 4px);
            left: 0;
            right: 0;
            background: white;
            border: 1px solid #cbd5e1;
            border-radius: 6px;
            box-shadow: 0 6px 18px rgba(0,0,0,0.12);
            max-height: 380px;
            overflow-y: auto;
            z-index: 100;
            display: none;
        }}

        .org-dropdown-menu.open {{ display: block; }}

        .org-dropdown-search {{
            padding: 12px;
            border-bottom: 1px solid #e2e8f0;
            position: sticky;
            top: 0;
            background: white;
        }}

        .org-dropdown-search input {{
            width: 100%;
            padding: 8px 10px;
            border: 1px solid #cbd5e1;
            border-radius: 4px;
            font-size: 0.9em;
        }}

        .org-dropdown-group-title {{
            padding: 8px 14px;
            font-size: 0.75em;
            font-weight: 700;
            color: #64748b;
            text-transform: uppercase;
            background: #f8fafc;
        }}

        .org-dropdown-item {{
            padding: 10px 16px;
            cursor: pointer;
            border-left: 3px solid transparent;
        }}

        .org-dropdown-item:hover {{ background: #eff6ff; border-left-color: #3b82f6; }}
        .org-dropdown-item.independent {{ border-left-color: #10b981; }}
        .org-dropdown-item.dependent {{ border-left-color: #f59e0b; }}
        .org-dropdown-item .item-name {{ font-weight: 600; color: #1e293b; }}
        .org-dropdown-item .item-meta {{ font-size: 0.8em; color: #64748b; margin-top: 2px; }}

        /* Org detail view */
        .org-detail-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
            padding-bottom: 16px;
            border-bottom: 1px solid #e2e8f0;
            margin-bottom: 18px;
        }}

        .org-detail-title {{
            font-size: 1.5em;
            font-weight: 700;
            color: #1e293b;
        }}

        .badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 0.78em;
            font-weight: 600;
        }}

        .badge.success {{ background: #d1fae5; color: #065f46; }}
        .badge.warning {{ background: #fef3c7; color: #92400e; }}
        .badge.danger {{ background: #fee2e2; color: #991b1b; }}

        .resource-summary {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 20px;
        }}

        .resource-chip {{
            background: #f1f5f9;
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.85em;
            color: #475569;
        }}

        .resource-chip strong {{ color: #1e293b; }}

        /* Dependency list */
        .dep-org-block {{
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            margin-bottom: 14px;
            overflow: hidden;
        }}

        .dep-org-header {{
            background: #fef3c7;
            padding: 12px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            cursor: pointer;
            user-select: none;
        }}

        .dep-org-header:hover {{ background: #fde68a; }}

        .dep-org-name {{ font-weight: 700; color: #92400e; }}
        .dep-org-count {{ font-size: 0.85em; color: #92400e; }}
        .dep-toggle {{ color: #92400e; font-weight: 700; font-size: 0.9em; }}

        .dep-resources-list {{
            list-style: none;
            padding: 0;
            background: white;
            display: none;
        }}

        .dep-resources-list.open {{ display: block; }}

        .dep-resource {{
            padding: 12px 18px;
            border-top: 1px solid #f1f5f9;
        }}

        .dep-resource-header {{
            display: flex;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 6px;
        }}

        .dep-resource-name {{ font-weight: 600; color: #1e293b; }}
        .dep-resource-meta {{ font-size: 0.8em; color: #64748b; }}

        .used-by-label {{
            font-size: 0.8em;
            color: #64748b;
            font-weight: 600;
            text-transform: uppercase;
            margin-top: 6px;
            margin-bottom: 4px;
        }}

        .used-by-list {{
            list-style: none;
            padding-left: 0;
        }}

        .used-by-list li {{
            padding: 4px 0 4px 16px;
            position: relative;
            font-size: 0.88em;
            color: #475569;
        }}

        .used-by-list li::before {{
            content: "→";
            position: absolute;
            left: 0;
            color: #94a3b8;
        }}

        .used-by-list li strong {{ color: #1e293b; }}

        .empty-state {{
            text-align: center;
            padding: 40px 20px;
            color: #94a3b8;
        }}

        .empty-state .empty-icon {{
            font-size: 3em;
            margin-bottom: 12px;
        }}

        .placeholder {{
            text-align: center;
            padding: 80px 20px;
            color: #94a3b8;
            background: white;
            border-radius: 10px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        }}

        .placeholder-icon {{ font-size: 3em; margin-bottom: 16px; }}

        code {{
            background: #f1f5f9;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: 'SF Mono', Menlo, Consolas, monospace;
            font-size: 0.88em;
            color: #1e293b;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>AAP Migration — Dependency Analysis</h1>
            <div class="subtitle">Source: {source_url} &nbsp;·&nbsp; Analyzed: {analysis_date}</div>
            <div class="header-controls">
                <div class="search-box">
                    <input type="text" class="search-input" id="globalSearch"
                           placeholder="Search organizations..." onkeyup="globalSearch()">
                </div>
                <div class="stats-bar">
                    <div class="stat-item"><strong>{total_orgs}</strong> Orgs</div>
                    <div class="stat-item"><strong>{indep_count}</strong> Independent</div>
                    <div class="stat-item"><strong>{dep_count}</strong> Dependent</div>
                    <div class="stat-item"><strong>{phase_count}</strong> Phases</div>
                    <div class="stat-item"><strong>{cycle_count}</strong> Cycles</div>
                </div>
            </div>
        </div>

        <div class="tabs">
            <button class="tab active" data-tab="overview" onclick="switchTab(event, 'overview')">Overview</button>
            <button class="tab" data-tab="phases" onclick="switchTab(event, 'phases')">Migration Phases</button>
            <button class="tab" data-tab="organizations" onclick="switchTab(event, 'organizations')">Organizations</button>
        </div>

        <div class="main-content">
            <!-- Overview -->
            <div class="tab-content active" id="tab-overview">
                <div class="stat-grid">
                    <div class="stat-card"><div class="stat-value">{total_orgs}</div>
                        <div class="stat-label">Total Organizations</div></div>
                    <div class="stat-card success"><div class="stat-value">{indep_count}</div>
                        <div class="stat-label">Independent</div></div>
                    <div class="stat-card warning"><div class="stat-value">{dep_count}</div>
                        <div class="stat-label">With Dependencies</div></div>
                    <div class="stat-card"><div class="stat-value">{phase_count}</div>
                        <div class="stat-label">Migration Phases</div></div>
                    <div class="stat-card danger"><div class="stat-value">{cycle_count}</div>
                        <div class="stat-label">Cycles Detected</div></div>
                </div>

                <div id="cyclesSection"></div>

                <div class="summary-grid">
                    <div class="card">
                        <h3>Independent Organizations</h3>
                        <p class="muted" style="margin-bottom: 12px;">
                            Can be migrated standalone (no cross-org dependencies).
                        </p>
                        <ul class="org-list" id="independentList"></ul>
                    </div>
                    <div class="card">
                        <h3>Organizations With Dependencies</h3>
                        <p class="muted" style="margin-bottom: 12px;">
                            Require other organizations to be migrated first.
                        </p>
                        <ul class="org-list" id="dependentList"></ul>
                    </div>
                </div>
            </div>

            <!-- Migration Phases -->
            <div class="tab-content" id="tab-phases">
                <h2>Migration Phases</h2>
                <p class="muted" style="margin-bottom: 22px;">
                    Organizations in the same phase can be migrated in parallel.
                    Click any organization to view its dependencies.
                </p>
                <div class="phases-grid" id="phasesGrid"></div>
            </div>

            <!-- Organizations -->
            <div class="tab-content" id="tab-organizations">
                <div class="org-selector">
                    <h3 style="margin-bottom: 12px;">Select Organization</h3>
                    <div class="org-dropdown">
                        <button class="org-dropdown-btn" onclick="toggleOrgDropdown()">
                            <span id="selectedOrg">Select an organization…</span>
                            <span>▾</span>
                        </button>
                        <div class="org-dropdown-menu" id="orgDropdownMenu">
                            <div class="org-dropdown-search">
                                <input type="text" id="orgSearchInput"
                                       placeholder="Filter organizations..."
                                       onkeyup="filterOrgDropdown()">
                            </div>
                            <div id="orgDropdownContent"></div>
                        </div>
                    </div>
                </div>

                <div id="orgDetail">
                    <div class="placeholder">
                        <div class="placeholder-icon">📋</div>
                        <div>Select an organization above to view its dependencies.</div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const orgsData = {orgs_json};
        const phasesData = {phases_json};
        const cyclesData = {cycles_json};

        // ---- utility ----
        function escapeHtml(s) {{
            if (s === null || s === undefined) return '';
            return String(s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;')
                .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }}

        function orgByName(name) {{
            return orgsData.find(o => o.name === name);
        }}

        function orgInCycle(name) {{
            return cyclesData.some(c => c.includes(name));
        }}

        // ---- init ----
        window.addEventListener('load', () => {{
            renderCycles();
            renderOverviewLists();
            renderPhases();
            renderOrgDropdown();
        }});

        window.addEventListener('click', (e) => {{
            if (!e.target.closest('.org-dropdown')) {{
                document.getElementById('orgDropdownMenu').classList.remove('open');
            }}
        }});

        // ---- tabs ----
        function switchTab(event, tabName) {{
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            event.currentTarget.classList.add('active');
            document.getElementById('tab-' + tabName).classList.add('active');
        }}

        function activateTab(tabName) {{
            document.querySelectorAll('.tab').forEach(t => {{
                t.classList.toggle('active', t.dataset.tab === tabName);
            }});
            document.querySelectorAll('.tab-content').forEach(c => {{
                c.classList.toggle('active', c.id === 'tab-' + tabName);
            }});
        }}

        // ---- search ----
        function globalSearch() {{
            const q = document.getElementById('globalSearch').value.toLowerCase().trim();
            if (!q) return;
            const org = orgsData.find(o => o.name.toLowerCase().includes(q));
            if (org) {{
                activateTab('organizations');
                selectOrg(org.name);
            }}
        }}

        // ---- cycles ----
        function renderCycles() {{
            const section = document.getElementById('cyclesSection');
            if (!cyclesData || cyclesData.length === 0) {{
                section.innerHTML = '';
                return;
            }}

            let html = '<div class="cycles-card">';
            html += '<h3>⚠️ Cyclic Dependencies Detected</h3>';
            html += '<p class="muted" style="margin-bottom: 8px;">';
            html += 'These organizations have mutual cross-references and cannot be ordered. ';
            html += 'They must be migrated as a unit, or the cross-references must be broken in the source.';
            html += '</p>';
            cyclesData.forEach((cycle, i) => {{
                html += '<div class="cycle-item">';
                html += '<div class="cycle-label">Cycle ' + (i + 1) + ' — ' + cycle.length + ' orgs</div>';
                html += '<ul class="cycle-orgs">';
                cycle.forEach(name => {{
                    html += '<li onclick="activateTab(\\'organizations\\'); selectOrg(\\''
                          + escapeHtml(name).replace(/'/g, "\\\\'") + '\\')">'
                          + escapeHtml(name) + '</li>';
                }});
                html += '</ul>';
                html += '</div>';
            }});
            html += '</div>';
            section.innerHTML = html;
        }}

        // ---- overview lists ----
        function renderOverviewLists() {{
            const indList = document.getElementById('independentList');
            const depList = document.getElementById('dependentList');

            const indOrgs = orgsData.filter(o => !o.has_dependencies);
            const depOrgs = orgsData.filter(o => o.has_dependencies);

            if (indOrgs.length === 0) {{
                indList.innerHTML = '<li class="empty-state" style="padding: 20px;">None</li>';
            }} else {{
                indList.innerHTML = indOrgs.map(o =>
                    '<li class="independent" onclick="activateTab(\\'organizations\\'); selectOrg(\\''
                    + o.name.replace(/'/g, "\\\\'") + '\\')">'
                    + '<div class="org-name">' + escapeHtml(o.name) + '</div>'
                    + '<div class="org-meta">' + o.total_resources + ' resources</div>'
                    + '</li>'
                ).join('');
            }}

            if (depOrgs.length === 0) {{
                depList.innerHTML = '<li class="empty-state" style="padding: 20px;">None</li>';
            }} else {{
                depList.innerHTML = depOrgs.map(o => {{
                    const reqs = o.required_before.length > 0
                        ? ' · Requires: ' + o.required_before.map(escapeHtml).join(', ')
                        : '';
                    return '<li class="dependent" onclick="activateTab(\\'organizations\\'); selectOrg(\\''
                        + o.name.replace(/'/g, "\\\\'") + '\\')">'
                        + '<div class="org-name">' + escapeHtml(o.name) + '</div>'
                        + '<div class="org-meta">' + o.total_resources + ' resources' + reqs + '</div>'
                        + '</li>';
                }}).join('');
            }}
        }}

        // ---- phases ----
        function renderPhases() {{
            const grid = document.getElementById('phasesGrid');
            grid.innerHTML = phasesData.map(phase => {{
                const orgItems = phase.orgs.map(name => {{
                    const o = orgByName(name);
                    if (!o) return '';
                    const classes = ['', o.has_dependencies ? 'has-deps' : '', orgInCycle(name) ? 'in-cycle' : '']
                                    .filter(Boolean).join(' ');
                    const reqs = o.has_dependencies && o.required_before.length > 0
                        ? ' · Requires: ' + o.required_before.map(escapeHtml).join(', ')
                        : '';
                    return '<li class="' + classes + '" onclick="activateTab(\\'organizations\\'); selectOrg(\\''
                        + name.replace(/'/g, "\\\\'") + '\\')">'
                        + '<div class="org-name">' + escapeHtml(name) + '</div>'
                        + '<div class="org-meta">' + o.total_resources + ' resources' + reqs + '</div>'
                        + '</li>';
                }}).join('');

                const cardClass = phase.has_cycle ? 'phase-card has-cycle' : 'phase-card';
                return '<div class="' + cardClass + '">'
                    + '<div class="phase-header">Phase ' + phase.phase + '</div>'
                    + '<div class="phase-description">' + escapeHtml(phase.description) + '</div>'
                    + '<ul class="phase-orgs">' + orgItems + '</ul>'
                    + '</div>';
            }}).join('');
        }}

        // ---- org dropdown ----
        function renderOrgDropdown() {{
            const content = document.getElementById('orgDropdownContent');
            const indOrgs = orgsData.filter(o => !o.has_dependencies);
            const depOrgs = orgsData.filter(o => o.has_dependencies);

            let html = '';
            if (indOrgs.length > 0) {{
                html += '<div class="org-dropdown-group-title">✓ Independent (' + indOrgs.length + ')</div>';
                html += indOrgs.map(o => dropdownItem(o, 'independent')).join('');
            }}
            if (depOrgs.length > 0) {{
                html += '<div class="org-dropdown-group-title">⚠ With Dependencies (' + depOrgs.length + ')</div>';
                html += depOrgs.map(o => dropdownItem(o, 'dependent')).join('');
            }}
            content.innerHTML = html;
        }}

        function dropdownItem(org, cls) {{
            return '<div class="org-dropdown-item ' + cls + '" onclick="selectOrg(\\''
                + org.name.replace(/'/g, "\\\\'") + '\\')">'
                + '<div class="item-name">' + escapeHtml(org.name) + '</div>'
                + '<div class="item-meta">' + org.total_resources + ' resources</div>'
                + '</div>';
        }}

        function toggleOrgDropdown() {{
            document.getElementById('orgDropdownMenu').classList.toggle('open');
        }}

        function filterOrgDropdown() {{
            const q = document.getElementById('orgSearchInput').value.toLowerCase();
            document.querySelectorAll('.org-dropdown-item').forEach(item => {{
                const txt = item.textContent.toLowerCase();
                item.style.display = txt.includes(q) ? 'block' : 'none';
            }});
            document.querySelectorAll('.org-dropdown-group-title').forEach(t => {{
                t.style.display = q ? 'none' : 'block';
            }});
        }}

        // ---- org detail ----
        function selectOrg(name) {{
            const org = orgByName(name);
            if (!org) return;

            document.getElementById('selectedOrg').textContent = name;
            document.getElementById('orgDropdownMenu').classList.remove('open');
            document.getElementById('orgDetail').innerHTML = renderOrgDetail(org);
        }}

        function renderOrgDetail(org) {{
            const inCycle = orgInCycle(org.name);
            let badge = '';
            if (inCycle) {{
                badge = '<span class="badge danger">In Cycle</span>';
            }} else if (org.has_dependencies) {{
                badge = '<span class="badge warning">' + org.dependencies.length + ' Dependencies</span>';
            }} else {{
                badge = '<span class="badge success">Standalone</span>';
            }}

            let html = '<div class="card">';
            html += '<div class="org-detail-header">';
            html += '<div>';
            html += '<div class="org-detail-title">' + escapeHtml(org.name) + '</div>';
            html += '<div class="muted">ID ' + org.id + ' · ' + org.total_resources + ' total resources</div>';
            html += '</div>';
            html += '<div>' + badge + '</div>';
            html += '</div>';

            // Resource summary chips
            if (org.resource_summary.length > 0) {{
                html += '<div class="resource-summary">';
                org.resource_summary.forEach(r => {{
                    html += '<div class="resource-chip">'
                          + escapeHtml(r.display) + ': <strong>' + r.count + '</strong>'
                          + '</div>';
                }});
                html += '</div>';
            }}

            // Dependencies
            html += '<h3 style="margin-top: 8px;">Cross-Org Dependencies</h3>';

            if (!org.has_dependencies) {{
                html += '<div class="empty-state" style="padding: 24px;">'
                      + '<div class="empty-icon">✓</div>'
                      + '<div>No cross-organization dependencies.</div>'
                      + '<div class="muted" style="margin-top: 8px;">'
                      + 'Can be migrated standalone: <code>aap-bridge migrate -o &quot;'
                      + escapeHtml(org.name) + '&quot;</code>'
                      + '</div>'
                      + '</div>';
            }} else {{
                org.dependencies.forEach((dep, idx) => {{
                    const blockId = 'dep-' + idx;
                    html += '<div class="dep-org-block">';
                    html += '<div class="dep-org-header" onclick="toggleDepBlock(\\'' + blockId + '\\')">';
                    html += '<div>';
                    html += '<div class="dep-org-name">Depends on: ' + escapeHtml(dep.org) + '</div>';
                    html += '<div class="dep-org-count">' + dep.resource_count
                          + ' resource' + (dep.resource_count === 1 ? '' : 's') + ' required</div>';
                    html += '</div>';
                    html += '<div class="dep-toggle" id="' + blockId + '-toggle">▾</div>';
                    html += '</div>';
                    html += '<ul class="dep-resources-list open" id="' + blockId + '">';
                    dep.resources.forEach(res => {{
                        html += '<li class="dep-resource">';
                        html += '<div class="dep-resource-header">';
                        html += '<div>';
                        html += '<span class="dep-resource-name">' + escapeHtml(res.name) + '</span>';
                        html += ' <span class="dep-resource-meta">'
                              + escapeHtml(res.type_display) + ' · ID ' + res.id + '</span>';
                        html += '</div>';
                        html += '</div>';
                        if (res.used_by && res.used_by.length > 0) {{
                            html += '<div class="used-by-label">Required by</div>';
                            html += '<ul class="used-by-list">';
                            res.used_by.forEach(u => {{
                                html += '<li>' + escapeHtml(u.type) + ': <strong>'
                                      + escapeHtml(u.name) + '</strong>'
                                      + ' <span class="dep-resource-meta">(ID ' + u.id + ')</span>'
                                      + '</li>';
                            }});
                            html += '</ul>';
                        }}
                        html += '</li>';
                    }});
                    html += '</ul>';
                    html += '</div>';
                }});

                // Migration order hint
                html += '<div style="margin-top: 18px; padding: 14px 18px; background: #eff6ff; '
                     + 'border-left: 3px solid #3b82f6; border-radius: 4px;">';
                html += '<div style="font-weight: 600; color: #1e40af; margin-bottom: 6px;">Recommended migration order:</div>';
                html += '<ol style="padding-left: 22px; color: #1e40af;">';
                org.required_before.forEach(name => {{
                    html += '<li><code>aap-bridge migrate -o &quot;' + escapeHtml(name) + '&quot;</code></li>';
                }});
                html += '<li><code>aap-bridge migrate -o &quot;' + escapeHtml(org.name) + '&quot;</code></li>';
                html += '</ol>';
                html += '</div>';
            }}

            html += '</div>';
            return html;
        }}

        function toggleDepBlock(id) {{
            const block = document.getElementById(id);
            const toggle = document.getElementById(id + '-toggle');
            const isOpen = block.classList.toggle('open');
            toggle.textContent = isOpen ? '▾' : '▸';
        }}
    </script>
</body>
</html>"""
