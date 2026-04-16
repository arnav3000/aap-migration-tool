"""Self-contained HTML mind map generator for dependency analysis.

All CSS, JavaScript, and assets are embedded inline for air-gapped environments.
No external dependencies, CDNs, or internet connection required.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aap_migration.analysis.dependency_analyzer import GlobalDependencyReport


def generate_html_report(report: GlobalDependencyReport) -> str:
    """Generate self-contained HTML mind map report.

    Args:
        report: Global dependency analysis report

    Returns:
        Complete HTML string with embedded CSS/JS
    """
    # Build organization data with all resources
    orgs_data = []
    for org_name, org_report in report.org_reports.items():
        # Build resource tree for mind map
        resource_tree = {}
        for rtype, items in org_report.resources.items():
            if not items:
                continue

            # Friendly names for resource types
            type_display_name = {
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
            }.get(rtype, rtype.replace("_", " ").title())

            resource_tree[type_display_name] = []
            for item in items:
                resource_info = {
                    "id": item.get("id"),
                    "name": item.get("name", f"ID {item.get('id')}"),
                    "type": rtype,
                }

                # Check if this resource has cross-org dependencies
                has_cross_org_dep = False
                dep_details = []

                for dep_org, deps in org_report.dependencies.items():
                    for dep in deps:
                        # Check if this resource uses the dependency
                        for usage in dep.required_by:
                            if usage["id"] == item.get("id") and usage["type"] == rtype:
                                has_cross_org_dep = True
                                dep_details.append({
                                    "org": dep_org,
                                    "resource_type": dep.resource_type,
                                    "resource_name": dep.resource_name,
                                    "resource_id": dep.resource_id,
                                })

                if has_cross_org_dep:
                    resource_info["cross_org_deps"] = dep_details

                resource_tree[type_display_name].append(resource_info)

        orgs_data.append({
            "name": org_name,
            "id": org_report.org_id,
            "total_resources": org_report.resource_count,
            "has_dependencies": org_report.has_cross_org_deps,
            "required_before": org_report.required_migrations_before,
            "resource_tree": resource_tree,
            "dependencies": [
                {
                    "org": dep_org,
                    "resources": [
                        {
                            "type": dep.resource_type,
                            "name": dep.resource_name,
                            "id": dep.resource_id,
                            "used_by": dep.required_by,
                        }
                        for dep in deps
                    ],
                }
                for dep_org, deps in org_report.dependencies.items()
            ],
        })

    # Build org-to-org edges for overview graph
    edges_data = []
    for org_name, org_report in report.org_reports.items():
        for dep_org in org_report.required_migrations_before:
            edges_data.append({"from": dep_org, "to": org_name})

    # Build phases
    phases_data = []
    for phase in report.migration_phases:
        phases_data.append({
            "phase": phase["phase"],
            "description": phase["description"],
            "orgs": phase["orgs"],
        })

    # Serialize data to JSON for embedding
    orgs_json = json.dumps(orgs_data, indent=2)
    edges_json = json.dumps(edges_data, indent=2)
    phases_json = json.dumps(phases_data, indent=2)

    # Generate HTML
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AAP Migration Mind Map - Dependency Analysis</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1e3a8a 0%, #7c3aed 100%);
            overflow: hidden;
            height: 100vh;
        }}

        .container {{
            display: grid;
            grid-template-rows: auto auto 1fr;
            height: 100vh;
            background: white;
        }}

        .header {{
            background: linear-gradient(135deg, #1e3a8a 0%, #7c3aed 100%);
            color: white;
            padding: 20px 40px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
            z-index: 100;
        }}

        .header h1 {{
            font-size: 1.8em;
            margin-bottom: 5px;
        }}

        .header .subtitle {{
            font-size: 0.9em;
            opacity: 0.9;
        }}

        .header-controls {{
            display: flex;
            gap: 20px;
            align-items: center;
            margin-top: 15px;
        }}

        .search-box {{
            flex: 1;
            max-width: 400px;
        }}

        .search-input {{
            width: 100%;
            padding: 10px 15px;
            border: none;
            border-radius: 8px;
            font-size: 0.9em;
            background: rgba(255,255,255,0.95);
        }}

        .stats-bar {{
            display: flex;
            gap: 30px;
            font-size: 0.85em;
        }}

        .stat-item {{
            display: flex;
            align-items: center;
            gap: 5px;
        }}

        .tabs {{
            display: flex;
            background: #f1f5f9;
            border-bottom: 2px solid #cbd5e1;
            padding: 0 40px;
        }}

        .tab {{
            padding: 15px 30px;
            cursor: pointer;
            border: none;
            background: none;
            font-size: 1em;
            font-weight: 600;
            color: #64748b;
            border-bottom: 3px solid transparent;
            transition: all 0.2s;
        }}

        .tab:hover {{
            background: rgba(59, 130, 246, 0.1);
            color: #3b82f6;
        }}

        .tab.active {{
            color: #3b82f6;
            border-bottom-color: #3b82f6;
            background: white;
        }}

        .main-content {{
            overflow-y: auto;
            background: #f8fafc;
        }}

        .tab-content {{
            display: none;
            padding: 40px;
            animation: fadeIn 0.3s;
        }}

        .tab-content.active {{
            display: block;
        }}

        /* Overview Tab */
        .overview-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 40px;
        }}

        .stat-card {{
            background: white;
            padding: 25px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            text-align: center;
        }}

        .stat-value {{
            font-size: 3em;
            font-weight: 700;
            color: #3b82f6;
        }}

        .stat-label {{
            font-size: 0.9em;
            color: #64748b;
            margin-top: 10px;
        }}

        .stat-card.success .stat-value {{
            color: #10b981;
        }}

        .stat-card.warning .stat-value {{
            color: #f59e0b;
        }}

        .overview-graph {{
            background: white;
            padding: 30px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .overview-graph h3 {{
            margin-bottom: 20px;
            color: #1e293b;
        }}

        /* Migration Phases Tab */
        .phases-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 25px;
        }}

        .phase-card {{
            background: white;
            border-radius: 12px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            overflow: hidden;
            transition: all 0.3s;
        }}

        .phase-card:hover {{
            transform: translateY(-5px);
            box-shadow: 0 8px 20px rgba(0,0,0,0.15);
        }}

        .phase-header {{
            background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%);
            color: white;
            padding: 20px;
            font-weight: 700;
            font-size: 1.3em;
        }}

        .phase-description {{
            padding: 15px 20px;
            background: #eff6ff;
            color: #1e40af;
            font-size: 0.9em;
            font-weight: 500;
        }}

        .phase-orgs {{
            padding: 20px;
        }}

        .phase-org-item {{
            padding: 12px 15px;
            margin: 8px 0;
            background: #f8fafc;
            border-left: 4px solid #3b82f6;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.2s;
        }}

        .phase-org-item:hover {{
            background: #eff6ff;
            transform: translateX(5px);
            box-shadow: 0 2px 8px rgba(59, 130, 246, 0.2);
        }}

        .phase-org-item.has-deps {{
            border-left-color: #f59e0b;
        }}

        .phase-org-name {{
            font-weight: 600;
            color: #1e293b;
        }}

        .phase-org-meta {{
            font-size: 0.8em;
            color: #64748b;
            margin-top: 3px;
        }}

        /* Organizations Tab */
        .org-selector {{
            background: white;
            padding: 30px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            margin-bottom: 30px;
        }}

        .org-dropdown {{
            position: relative;
            max-width: 500px;
        }}

        .org-dropdown-btn {{
            width: 100%;
            padding: 15px 20px;
            background: white;
            border: 2px solid #cbd5e1;
            border-radius: 8px;
            font-size: 1em;
            cursor: pointer;
            text-align: left;
            display: flex;
            justify-content: space-between;
            align-items: center;
            transition: all 0.2s;
        }}

        .org-dropdown-btn:hover {{
            border-color: #3b82f6;
        }}

        .org-dropdown-menu {{
            position: absolute;
            top: 100%;
            left: 0;
            right: 0;
            margin-top: 5px;
            background: white;
            border: 2px solid #cbd5e1;
            border-radius: 8px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.15);
            max-height: 400px;
            overflow-y: auto;
            z-index: 1000;
            display: none;
        }}

        .org-dropdown-menu.open {{
            display: block;
        }}

        .org-dropdown-search {{
            padding: 15px;
            border-bottom: 1px solid #e2e8f0;
            sticky: top;
            background: white;
        }}

        .org-dropdown-search input {{
            width: 100%;
            padding: 10px;
            border: 1px solid #cbd5e1;
            border-radius: 6px;
            font-size: 0.9em;
        }}

        .org-dropdown-group {{
            padding: 10px 0;
        }}

        .org-dropdown-group-title {{
            padding: 10px 15px;
            font-size: 0.8em;
            font-weight: 700;
            color: #64748b;
            text-transform: uppercase;
            background: #f8fafc;
        }}

        .org-dropdown-item {{
            padding: 12px 20px;
            cursor: pointer;
            transition: all 0.2s;
            border-left: 3px solid transparent;
        }}

        .org-dropdown-item:hover {{
            background: #eff6ff;
            border-left-color: #3b82f6;
        }}

        .org-dropdown-item.independent {{
            border-left-color: #10b981;
        }}

        .org-dropdown-item.dependent {{
            border-left-color: #f59e0b;
        }}

        /* Mind Map Canvas */
        .mindmap-container {{
            background: white;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            overflow: hidden;
            position: relative;
            min-height: 600px;
        }}

        .mindmap-title {{
            padding: 20px 30px;
            background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%);
            color: white;
            font-size: 1.3em;
            font-weight: 700;
        }}

        .mindmap-controls {{
            padding: 15px 30px;
            background: #f8fafc;
            border-bottom: 1px solid #e2e8f0;
            display: flex;
            gap: 10px;
        }}

        .control-btn {{
            padding: 8px 15px;
            background: #3b82f6;
            color: white;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.85em;
            transition: all 0.2s;
        }}

        .control-btn:hover {{
            background: #2563eb;
        }}

        .control-btn.secondary {{
            background: #64748b;
        }}

        .control-btn.secondary:hover {{
            background: #475569;
        }}

        .mindmap-canvas {{
            padding: 30px;
            overflow: auto;
            max-height: 700px;
        }}

        .mindmap-svg {{
            width: 100%;
            height: 100%;
            min-height: 600px;
        }}

        .placeholder {{
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 100px 20px;
            color: #94a3b8;
        }}

        .placeholder-icon {{
            font-size: 4em;
            margin-bottom: 20px;
        }}

        .placeholder-text {{
            font-size: 1.2em;
            text-align: center;
        }}

        /* SVG Nodes */
        .node-group {{
            cursor: pointer;
            transition: all 0.3s;
        }}

        .node-group:hover {{
            filter: brightness(1.1);
        }}

        .org-node circle {{
            fill: #3b82f6;
            stroke: #1e40af;
            stroke-width: 4;
        }}

        .type-node circle {{
            fill: #8b5cf6;
            stroke: #6d28d9;
            stroke-width: 3;
        }}

        .type-node.collapsed circle {{
            fill: #64748b;
            stroke: #475569;
        }}

        .resource-node circle {{
            fill: #10b981;
            stroke: #059669;
            stroke-width: 2;
        }}

        .resource-node.has-cross-org circle {{
            fill: #ef4444;
            stroke: #dc2626;
            stroke-width: 3;
        }}

        .resource-node.hidden {{
            display: none;
        }}

        .node-text {{
            font-size: 12px;
            font-weight: 600;
            fill: #1e293b;
            pointer-events: none;
            text-anchor: middle;
        }}

        .node-text-small {{
            font-size: 10px;
            fill: white;
            text-anchor: middle;
            pointer-events: none;
        }}

        .link {{
            fill: none;
            stroke: #cbd5e1;
            stroke-width: 2;
        }}

        .link.hidden {{
            display: none;
        }}

        .link-cross-org {{
            fill: none;
            stroke: #ef4444;
            stroke-width: 3;
            stroke-dasharray: 5,5;
        }}

        .overview-link {{
            fill: none;
            stroke: #94a3af;
            stroke-width: 2;
            marker-end: url(#arrowhead);
        }}

        .overview-node {{
            cursor: pointer;
        }}

        .overview-node circle {{
            transition: all 0.2s;
        }}

        .overview-node:hover circle {{
            filter: brightness(1.2);
        }}

        .overview-node.independent circle {{
            fill: #10b981;
            stroke: #059669;
            stroke-width: 3;
        }}

        .overview-node.dependent circle {{
            fill: #f59e0b;
            stroke: #d97706;
            stroke-width: 3;
        }}

        /* Tooltip */
        .tooltip {{
            position: absolute;
            background: rgba(0, 0, 0, 0.9);
            color: white;
            padding: 12px 16px;
            border-radius: 8px;
            font-size: 0.85em;
            pointer-events: none;
            z-index: 2000;
            max-width: 350px;
            display: none;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }}

        .tooltip h4 {{
            margin-bottom: 8px;
            color: #60a5fa;
            font-size: 1.1em;
        }}

        .tooltip-row {{
            margin: 5px 0;
            line-height: 1.4;
        }}

        .tooltip-deps {{
            margin-top: 10px;
            padding-top: 10px;
            border-top: 1px solid rgba(255,255,255,0.2);
        }}

        @keyframes fadeIn {{
            from {{ opacity: 0; }}
            to {{ opacity: 1; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🧠 AAP Migration Mind Map - Dependency Analysis</h1>
            <div class="header-controls">
                <div class="search-box">
                    <input type="text" class="search-input" id="globalSearch" placeholder="🔍 Search organizations..." onkeyup="globalSearch()">
                </div>
                <div class="stats-bar">
                    <div class="stat-item">📊 <strong>{report.total_organizations}</strong> Orgs</div>
                    <div class="stat-item">✅ <strong>{len(report.independent_orgs)}</strong> Independent</div>
                    <div class="stat-item">⚠️ <strong>{len(report.dependent_orgs)}</strong> Dependent</div>
                    <div class="stat-item">📈 <strong>{len(report.migration_phases)}</strong> Phases</div>
                </div>
            </div>
        </div>

        <div class="tabs">
            <button class="tab active" onclick="switchTab('overview')">Overview</button>
            <button class="tab" onclick="switchTab('phases')">Migration Phases</button>
            <button class="tab" onclick="switchTab('organizations')">Organizations</button>
        </div>

        <div class="main-content">
            <!-- Overview Tab -->
            <div class="tab-content active" id="tab-overview">
                <div class="overview-grid">
                    <div class="stat-card">
                        <div class="stat-value">{report.total_organizations}</div>
                        <div class="stat-label">Total Organizations</div>
                    </div>
                    <div class="stat-card success">
                        <div class="stat-value">{len(report.independent_orgs)}</div>
                        <div class="stat-label">Independent Organizations</div>
                    </div>
                    <div class="stat-card warning">
                        <div class="stat-value">{len(report.dependent_orgs)}</div>
                        <div class="stat-label">Organizations with Dependencies</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value">{len(report.migration_phases)}</div>
                        <div class="stat-label">Migration Phases</div>
                    </div>
                </div>

                <div class="overview-graph">
                    <h3>Organization Dependency Graph</h3>
                    <p style="color: #64748b; margin-bottom: 20px;">High-level view of cross-organization dependencies. Click any organization to view detailed mind map.</p>
                    <svg id="overviewSvg" style="width: 100%; height: 500px;"></svg>
                </div>
            </div>

            <!-- Migration Phases Tab -->
            <div class="tab-content" id="tab-phases">
                <h2 style="margin-bottom: 25px; color: #1e293b;">Migration Phases</h2>
                <p style="color: #64748b; margin-bottom: 30px;">Click any organization to view its detailed resource mind map below.</p>
                <div class="phases-grid" id="phasesGrid"></div>

                <div id="phaseMindMap" style="margin-top: 40px;"></div>
            </div>

            <!-- Organizations Tab -->
            <div class="tab-content" id="tab-organizations">
                <div class="org-selector">
                    <h2 style="margin-bottom: 15px; color: #1e293b;">Select Organization</h2>
                    <p style="color: #64748b; margin-bottom: 20px;">Choose an organization to view its detailed resource mind map.</p>

                    <div class="org-dropdown">
                        <button class="org-dropdown-btn" onclick="toggleOrgDropdown()">
                            <span id="selectedOrg">Select an organization...</span>
                            <span>▼</span>
                        </button>
                        <div class="org-dropdown-menu" id="orgDropdownMenu">
                            <div class="org-dropdown-search">
                                <input type="text" placeholder="Search organizations..." onkeyup="filterOrgDropdown()" id="orgSearchInput">
                            </div>
                            <div id="orgDropdownContent"></div>
                        </div>
                    </div>
                </div>

                <div id="orgMindMap"></div>
            </div>
        </div>

        <div class="tooltip" id="tooltip"></div>
    </div>

    <script>
        // Data
        const orgsData = {orgs_json};
        const edgesData = {edges_json};
        const phasesData = {phases_json};

        let currentOrg = null;
        let expandedTypes = {{}};

        // Initialize
        function init() {{
            renderPhasesGrid();
            renderOrgDropdown();
            renderOverviewGraph();
        }}

        // Tab switching
        function switchTab(tabName) {{
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

            event.target.classList.add('active');
            document.getElementById(`tab-${{tabName}}`).classList.add('active');
        }}

        // Global search
        function globalSearch() {{
            const query = document.getElementById('globalSearch').value.toLowerCase();
            if (!query) return;

            const org = orgsData.find(o => o.name.toLowerCase().includes(query));
            if (org) {{
                switchTab('organizations');
                selectOrgFromDropdown(org.name);
            }}
        }}

        // Render migration phases grid
        function renderPhasesGrid() {{
            const grid = document.getElementById('phasesGrid');
            grid.innerHTML = '';

            phasesData.forEach(phase => {{
                const card = document.createElement('div');
                card.className = 'phase-card';

                const header = document.createElement('div');
                header.className = 'phase-header';
                header.textContent = `Phase ${{phase.phase}}`;
                card.appendChild(header);

                const desc = document.createElement('div');
                desc.className = 'phase-description';
                desc.textContent = phase.description;
                card.appendChild(desc);

                const orgsDiv = document.createElement('div');
                orgsDiv.className = 'phase-orgs';

                phase.orgs.forEach(orgName => {{
                    const org = orgsData.find(o => o.name === orgName);
                    const item = document.createElement('div');
                    item.className = `phase-org-item ${{org.has_dependencies ? 'has-deps' : ''}}`;
                    item.onclick = () => {{
                        showMindMapInPhaseTab(orgName);
                    }};

                    const name = document.createElement('div');
                    name.className = 'phase-org-name';
                    name.textContent = orgName;
                    item.appendChild(name);

                    const meta = document.createElement('div');
                    meta.className = 'phase-org-meta';
                    meta.textContent = `${{org.total_resources}} resources`;
                    if (org.has_dependencies) {{
                        meta.textContent += ` • Requires: ${{org.required_before.join(', ')}}`;
                    }}
                    item.appendChild(meta);

                    orgsDiv.appendChild(item);
                }});

                card.appendChild(orgsDiv);
                grid.appendChild(card);
            }});
        }}

        function showMindMapInPhaseTab(orgName) {{
            currentOrg = orgsData.find(o => o.name === orgName);
            const container = document.getElementById('phaseMindMap');
            container.innerHTML = '';

            const mindmapDiv = createMindMapDiv(currentOrg);
            container.appendChild(mindmapDiv);
        }}

        // Render org dropdown
        function renderOrgDropdown() {{
            const content = document.getElementById('orgDropdownContent');
            content.innerHTML = '';

            // Independent orgs
            const indGroup = document.createElement('div');
            indGroup.className = 'org-dropdown-group';
            const indTitle = document.createElement('div');
            indTitle.className = 'org-dropdown-group-title';
            indTitle.textContent = `✅ Independent (${{orgsData.filter(o => !o.has_dependencies).length}})`;
            indGroup.appendChild(indTitle);

            orgsData.filter(o => !o.has_dependencies).forEach(org => {{
                const item = createOrgDropdownItem(org, 'independent');
                indGroup.appendChild(item);
            }});
            content.appendChild(indGroup);

            // Dependent orgs
            const depGroup = document.createElement('div');
            depGroup.className = 'org-dropdown-group';
            const depTitle = document.createElement('div');
            depTitle.className = 'org-dropdown-group-title';
            depTitle.textContent = `⚠️ With Dependencies (${{orgsData.filter(o => o.has_dependencies).length}})`;
            depGroup.appendChild(depTitle);

            orgsData.filter(o => o.has_dependencies).forEach(org => {{
                const item = createOrgDropdownItem(org, 'dependent');
                depGroup.appendChild(item);
            }});
            content.appendChild(depGroup);
        }}

        function createOrgDropdownItem(org, type) {{
            const item = document.createElement('div');
            item.className = `org-dropdown-item ${{type}}`;
            item.onclick = () => selectOrgFromDropdown(org.name);

            const html = `
                <div style="font-weight: 600; color: #1e293b;">${{org.name}}</div>
                <div style="font-size: 0.8em; color: #64748b; margin-top: 3px;">${{org.total_resources}} resources</div>
            `;
            item.innerHTML = html;
            return item;
        }}

        function toggleOrgDropdown() {{
            const menu = document.getElementById('orgDropdownMenu');
            menu.classList.toggle('open');
        }}

        function filterOrgDropdown() {{
            const query = document.getElementById('orgSearchInput').value.toLowerCase();
            const items = document.querySelectorAll('.org-dropdown-item');

            items.forEach(item => {{
                const text = item.textContent.toLowerCase();
                item.style.display = text.includes(query) ? 'block' : 'none';
            }});
        }}

        function selectOrgFromDropdown(orgName) {{
            currentOrg = orgsData.find(o => o.name === orgName);
            document.getElementById('selectedOrg').textContent = orgName;
            document.getElementById('orgDropdownMenu').classList.remove('open');

            const container = document.getElementById('orgMindMap');
            container.innerHTML = '';

            const mindmapDiv = createMindMapDiv(currentOrg);
            container.appendChild(mindmapDiv);
        }}

        // Create mind map div
        function createMindMapDiv(org) {{
            expandedTypes = {{}};
            Object.keys(org.resource_tree).forEach(type => {{
                expandedTypes[type] = true;
            }});

            const container = document.createElement('div');
            container.className = 'mindmap-container';

            const title = document.createElement('div');
            title.className = 'mindmap-title';
            title.textContent = `🧠 ${{org.name}} - Resource Mind Map`;
            container.appendChild(title);

            const controls = document.createElement('div');
            controls.className = 'mindmap-controls';
            controls.innerHTML = `
                <button class="control-btn" onclick="expandAllTypes()">Expand All</button>
                <button class="control-btn" onclick="collapseAllTypes()">Collapse All</button>
                <button class="control-btn secondary" onclick="showOnlyDependencies()">Show Only Dependencies</button>
                <button class="control-btn secondary" onclick="resetView()">Reset View</button>
            `;
            container.appendChild(controls);

            const canvas = document.createElement('div');
            canvas.className = 'mindmap-canvas';
            canvas.id = 'currentMindmapCanvas';
            container.appendChild(canvas);

            renderMindMap(org, canvas);
            return container;
        }}

        // Render mind map
        function renderMindMap(org, canvas) {{
            canvas.innerHTML = '';

            const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.setAttribute('class', 'mindmap-svg');
            svg.id = 'currentMindmapSvg';

            const width = 1600;
            const height = 1200;
            svg.setAttribute('viewBox', `0 0 ${{width}} ${{height}}`);

            const centerX = width / 2;
            const centerY = height / 2;

            // Draw org node at center
            const orgGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
            orgGroup.setAttribute('class', 'org-node node-group');

            const orgCircle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            orgCircle.setAttribute('cx', centerX);
            orgCircle.setAttribute('cy', centerY);
            orgCircle.setAttribute('r', '80');
            orgGroup.appendChild(orgCircle);

            const orgText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            orgText.setAttribute('x', centerX);
            orgText.setAttribute('y', centerY - 10);
            orgText.setAttribute('class', 'node-text');
            orgText.textContent = org.name.length > 20 ? org.name.substring(0, 18) + '...' : org.name;
            orgGroup.appendChild(orgText);

            const orgCount = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            orgCount.setAttribute('x', centerX);
            orgCount.setAttribute('y', centerY + 10);
            orgCount.setAttribute('class', 'node-text-small');
            orgCount.textContent = `${{org.total_resources}} resources`;
            orgGroup.appendChild(orgCount);

            svg.appendChild(orgGroup);

            // Draw resource types radially
            const resourceTypes = Object.keys(org.resource_tree);
            const angleStep = (2 * Math.PI) / Math.max(resourceTypes.length, 1);
            const typeRadius = 250;

            resourceTypes.forEach((typeName, typeIdx) => {{
                const angle = typeIdx * angleStep - Math.PI / 2;
                const typeX = centerX + typeRadius * Math.cos(angle);
                const typeY = centerY + typeRadius * Math.sin(angle);

                // Draw link to center
                const link = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                link.setAttribute('class', 'link');
                link.setAttribute('data-type', typeName);
                link.setAttribute('d', `M ${{centerX}} ${{centerY}} L ${{typeX}} ${{typeY}}`);
                svg.appendChild(link);

                // Draw type node
                const typeGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
                typeGroup.setAttribute('class', 'type-node node-group');
                typeGroup.setAttribute('data-type', typeName);
                typeGroup.onclick = () => toggleTypeExpansion(typeName);

                const typeCircle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                typeCircle.setAttribute('cx', typeX);
                typeCircle.setAttribute('cy', typeY);
                typeCircle.setAttribute('r', '45');
                typeGroup.appendChild(typeCircle);

                const typeText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                typeText.setAttribute('x', typeX);
                typeText.setAttribute('y', typeY - 5);
                typeText.setAttribute('class', 'node-text-small');
                typeText.textContent = typeName.length > 12 ? typeName.substring(0, 10) + '...' : typeName;
                typeGroup.appendChild(typeText);

                const typeCountText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                typeCountText.setAttribute('x', typeX);
                typeCountText.setAttribute('y', typeY + 8);
                typeCountText.setAttribute('class', 'node-text-small');
                typeCountText.textContent = org.resource_tree[typeName].length;
                typeGroup.appendChild(typeCountText);

                svg.appendChild(typeGroup);

                // Draw individual resources
                const resources = org.resource_tree[typeName];
                const resourceAngleStep = Math.PI / Math.max(resources.length + 1, 2);
                const resourceRadius = 150;

                resources.forEach((resource, resIdx) => {{
                    const resAngle = angle - Math.PI / 4 + (resIdx + 1) * resourceAngleStep;
                    const resX = typeX + resourceRadius * Math.cos(resAngle);
                    const resY = typeY + resourceRadius * Math.sin(resAngle);

                    // Draw link to type node
                    const resLink = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                    const linkClass = resource.cross_org_deps ? 'link-cross-org' : 'link';
                    resLink.setAttribute('class', linkClass);
                    resLink.setAttribute('data-type', typeName);
                    resLink.setAttribute('data-resource-id', resource.id);
                    resLink.setAttribute('d', `M ${{typeX}} ${{typeY}} L ${{resX}} ${{resY}}`);
                    svg.appendChild(resLink);

                    // Draw resource node
                    const resGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
                    const resClass = resource.cross_org_deps ? 'resource-node has-cross-org node-group' : 'resource-node node-group';
                    resGroup.setAttribute('class', resClass);
                    resGroup.setAttribute('data-type', typeName);
                    resGroup.setAttribute('data-resource-id', resource.id);

                    const resCircle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                    resCircle.setAttribute('cx', resX);
                    resCircle.setAttribute('cy', resY);
                    resCircle.setAttribute('r', '25');
                    resCircle.onmouseover = (e) => showTooltip(e, resource, typeName);
                    resCircle.onmouseout = hideTooltip;
                    resGroup.appendChild(resCircle);

                    const resText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                    resText.setAttribute('x', resX);
                    resText.setAttribute('y', resY + 40);
                    resText.setAttribute('class', 'node-text');
                    resText.setAttribute('font-size', '10');
                    const displayName = resource.name.length > 15 ? resource.name.substring(0, 13) + '...' : resource.name;
                    resText.textContent = displayName;
                    resGroup.appendChild(resText);

                    svg.appendChild(resGroup);
                }});
            }});

            canvas.appendChild(svg);
        }}

        // Toggle type expansion
        function toggleTypeExpansion(typeName) {{
            expandedTypes[typeName] = !expandedTypes[typeName];
            updateMindMapVisibility();
        }}

        function expandAllTypes() {{
            Object.keys(expandedTypes).forEach(type => {{
                expandedTypes[type] = true;
            }});
            updateMindMapVisibility();
        }}

        function collapseAllTypes() {{
            Object.keys(expandedTypes).forEach(type => {{
                expandedTypes[type] = false;
            }});
            updateMindMapVisibility();
        }}

        function showOnlyDependencies() {{
            const svg = document.getElementById('currentMindmapSvg');
            if (!svg) return;

            svg.querySelectorAll('.resource-node').forEach(node => {{
                if (!node.classList.contains('has-cross-org')) {{
                    node.classList.add('hidden');
                }}
            }});

            svg.querySelectorAll('.link').forEach(link => {{
                if (!link.classList.contains('link-cross-org')) {{
                    link.classList.add('hidden');
                }}
            }});
        }}

        function resetView() {{
            expandAllTypes();
            const svg = document.getElementById('currentMindmapSvg');
            if (!svg) return;

            svg.querySelectorAll('.hidden').forEach(el => {{
                el.classList.remove('hidden');
            }});
        }}

        function updateMindMapVisibility() {{
            const svg = document.getElementById('currentMindmapSvg');
            if (!svg) return;

            Object.keys(expandedTypes).forEach(typeName => {{
                const isExpanded = expandedTypes[typeName];

                // Update type node appearance
                const typeNode = svg.querySelector(`.type-node[data-type="${{typeName}}"]`);
                if (typeNode) {{
                    if (isExpanded) {{
                        typeNode.classList.remove('collapsed');
                    }} else {{
                        typeNode.classList.add('collapsed');
                    }}
                }}

                // Show/hide resources
                svg.querySelectorAll(`.resource-node[data-type="${{typeName}}"]`).forEach(node => {{
                    if (isExpanded) {{
                        node.classList.remove('hidden');
                    }} else {{
                        node.classList.add('hidden');
                    }}
                }});

                // Show/hide links
                svg.querySelectorAll(`.link[data-type="${{typeName}}"]`).forEach(link => {{
                    if (isExpanded) {{
                        link.classList.remove('hidden');
                    }} else {{
                        link.classList.add('hidden');
                    }}
                }});
            }});
        }}

        // Tooltip
        function showTooltip(event, resource, typeName) {{
            const tooltip = document.getElementById('tooltip');

            let html = `<h4>${{resource.name}}</h4>`;
            html += `<div class="tooltip-row"><strong>Type:</strong> ${{typeName}}</div>`;
            html += `<div class="tooltip-row"><strong>ID:</strong> ${{resource.id}}</div>`;

            if (resource.cross_org_deps) {{
                html += `<div class="tooltip-deps">`;
                html += `<div style="color: #fca5a5; font-weight: 600; margin-bottom: 5px;">⚠️ Cross-Org Dependencies:</div>`;
                resource.cross_org_deps.forEach(dep => {{
                    html += `<div class="tooltip-row">→ ${{dep.resource_type}}: <strong>${{dep.resource_name}}</strong></div>`;
                    html += `<div class="tooltip-row" style="margin-left: 15px; font-size: 0.9em; color: #cbd5e1;">from ${{dep.org}}</div>`;
                }});
                html += `</div>`;
            }}

            tooltip.innerHTML = html;
            tooltip.style.display = 'block';
            tooltip.style.left = (event.pageX + 15) + 'px';
            tooltip.style.top = (event.pageY + 15) + 'px';
        }}

        function hideTooltip() {{
            document.getElementById('tooltip').style.display = 'none';
        }}

        // Overview graph
        function renderOverviewGraph() {{
            const svg = document.getElementById('overviewSvg');
            if (!svg) return;

            const width = svg.clientWidth || 1000;
            const height = 500;

            // Create arrow marker
            const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
            const marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
            marker.setAttribute('id', 'arrowhead');
            marker.setAttribute('markerWidth', '10');
            marker.setAttribute('markerHeight', '10');
            marker.setAttribute('refX', '25');
            marker.setAttribute('refY', '3');
            marker.setAttribute('orient', 'auto');
            const polygon = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
            polygon.setAttribute('points', '0 0, 6 3, 0 6');
            polygon.setAttribute('fill', '#94a3af');
            marker.appendChild(polygon);
            defs.appendChild(marker);
            svg.appendChild(defs);

            // Layout nodes in circle
            const nodePositions = {{}};
            const centerX = width / 2;
            const centerY = height / 2;
            const radius = Math.min(width, height) * 0.35;

            const independentOrgs = orgsData.filter(o => !o.has_dependencies);
            const dependentOrgs = orgsData.filter(o => o.has_dependencies);

            // Position independent orgs in outer circle
            independentOrgs.forEach((org, i) => {{
                const angle = (i / independentOrgs.length) * 2 * Math.PI;
                nodePositions[org.name] = {{
                    x: centerX + radius * Math.cos(angle),
                    y: centerY + radius * Math.sin(angle)
                }};
            }});

            // Position dependent orgs in inner circle
            dependentOrgs.forEach((org, i) => {{
                const angle = (i / Math.max(dependentOrgs.length, 1)) * 2 * Math.PI;
                nodePositions[org.name] = {{
                    x: centerX + (radius * 0.5) * Math.cos(angle),
                    y: centerY + (radius * 0.5) * Math.sin(angle)
                }};
            }});

            // Draw edges
            edgesData.forEach(edge => {{
                const from = nodePositions[edge.from];
                const to = nodePositions[edge.to];
                if (from && to) {{
                    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                    path.setAttribute('class', 'overview-link');
                    path.setAttribute('d', `M ${{from.x}} ${{from.y}} L ${{to.x}} ${{to.y}}`);
                    svg.appendChild(path);
                }}
            }});

            // Draw nodes
            orgsData.forEach(org => {{
                const pos = nodePositions[org.name];
                if (!pos) return;

                const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
                g.setAttribute('class', `overview-node ${{org.has_dependencies ? 'dependent' : 'independent'}}`);
                g.onclick = () => {{
                    switchTab('organizations');
                    setTimeout(() => selectOrgFromDropdown(org.name), 100);
                }};

                const nodeRadius = 20;
                const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circle.setAttribute('cx', pos.x);
                circle.setAttribute('cy', pos.y);
                circle.setAttribute('r', nodeRadius);
                g.appendChild(circle);

                const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                text.setAttribute('x', pos.x);
                text.setAttribute('y', pos.y + nodeRadius + 15);
                text.setAttribute('class', 'node-text');
                text.setAttribute('font-size', '11');
                text.textContent = org.name.length > 15 ? org.name.substring(0, 13) + '...' : org.name;
                g.appendChild(text);

                svg.appendChild(g);
            }});
        }}

        // Initialize on load
        window.addEventListener('load', init);

        // Close dropdown when clicking outside
        window.addEventListener('click', (e) => {{
            if (!e.target.closest('.org-dropdown')) {{
                document.getElementById('orgDropdownMenu').classList.remove('open');
            }}
        }});
    </script>
</body>
</html>'''

    return html
