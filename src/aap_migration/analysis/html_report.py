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

    # Build org-to-org edges
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
            grid-template-rows: auto 1fr;
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

        .stats-bar {{
            display: flex;
            gap: 30px;
            margin-top: 15px;
            font-size: 0.85em;
        }}

        .stat-item {{
            display: flex;
            align-items: center;
            gap: 5px;
        }}

        .main-content {{
            display: grid;
            grid-template-columns: 350px 1fr;
            height: 100%;
            overflow: hidden;
        }}

        .sidebar {{
            background: #f8fafc;
            border-right: 2px solid #e2e8f0;
            overflow-y: auto;
            padding: 20px;
        }}

        .sidebar h2 {{
            font-size: 1.2em;
            margin-bottom: 15px;
            color: #1e293b;
        }}

        .org-list {{
            display: flex;
            flex-direction: column;
            gap: 10px;
        }}

        .org-item {{
            background: white;
            padding: 15px;
            border-radius: 8px;
            border: 2px solid #e2e8f0;
            cursor: pointer;
            transition: all 0.2s;
        }}

        .org-item:hover {{
            border-color: #3b82f6;
            transform: translateX(5px);
            box-shadow: 0 4px 12px rgba(59, 130, 246, 0.2);
        }}

        .org-item.active {{
            border-color: #3b82f6;
            background: #eff6ff;
            box-shadow: 0 4px 12px rgba(59, 130, 246, 0.3);
        }}

        .org-item.independent {{
            border-left: 4px solid #10b981;
        }}

        .org-item.dependent {{
            border-left: 4px solid #f59e0b;
        }}

        .org-item-name {{
            font-weight: 600;
            color: #1e293b;
            margin-bottom: 5px;
        }}

        .org-item-meta {{
            font-size: 0.8em;
            color: #64748b;
        }}

        .org-item-deps {{
            font-size: 0.75em;
            color: #f59e0b;
            margin-top: 5px;
            font-weight: 500;
        }}

        .canvas-area {{
            position: relative;
            background: #ffffff;
            overflow: hidden;
        }}

        .mindmap-canvas {{
            width: 100%;
            height: 100%;
            overflow: auto;
        }}

        .mindmap-svg {{
            width: 100%;
            height: 100%;
            min-width: 100%;
            min-height: 100%;
        }}

        .placeholder {{
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100%;
            color: #94a3b8;
        }}

        .placeholder-icon {{
            font-size: 4em;
            margin-bottom: 20px;
        }}

        .placeholder-text {{
            font-size: 1.2em;
        }}

        /* Mind map styling */
        .node-circle {{
            cursor: pointer;
            transition: all 0.3s;
        }}

        .node-circle:hover {{
            filter: brightness(1.1);
            stroke-width: 3;
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
            stroke: #94a3b8;
            stroke-width: 2;
        }}

        .link-cross-org {{
            fill: none;
            stroke: #ef4444;
            stroke-width: 3;
            stroke-dasharray: 5,5;
        }}

        .tooltip {{
            position: absolute;
            background: rgba(0, 0, 0, 0.9);
            color: white;
            padding: 10px 15px;
            border-radius: 6px;
            font-size: 0.85em;
            pointer-events: none;
            z-index: 1000;
            max-width: 300px;
            display: none;
        }}

        .tooltip h4 {{
            margin-bottom: 5px;
            color: #60a5fa;
        }}

        .tooltip-row {{
            margin: 3px 0;
        }}

        .controls {{
            position: absolute;
            top: 20px;
            right: 20px;
            background: white;
            padding: 15px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            z-index: 50;
        }}

        .control-btn {{
            display: block;
            width: 100%;
            padding: 8px 15px;
            margin: 5px 0;
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

        .legend {{
            position: absolute;
            bottom: 20px;
            left: 20px;
            background: white;
            padding: 15px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            font-size: 0.8em;
        }}

        .legend-title {{
            font-weight: 700;
            margin-bottom: 10px;
            color: #1e293b;
        }}

        .legend-item {{
            display: flex;
            align-items: center;
            gap: 8px;
            margin: 5px 0;
        }}

        .legend-color {{
            width: 16px;
            height: 16px;
            border-radius: 50%;
            border: 2px solid;
        }}

        .legend-color.org {{
            background: #3b82f6;
            border-color: #1e40af;
        }}

        .legend-color.type {{
            background: #8b5cf6;
            border-color: #6d28d9;
        }}

        .legend-color.resource {{
            background: #10b981;
            border-color: #059669;
        }}

        .legend-color.cross-org {{
            background: #ef4444;
            border-color: #dc2626;
        }}

        .phase-indicator {{
            position: absolute;
            top: 20px;
            left: 20px;
            background: white;
            padding: 10px 15px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            font-size: 0.85em;
            font-weight: 600;
            color: #1e293b;
        }}

        .phase-indicator .phase-number {{
            color: #3b82f6;
            font-size: 1.2em;
        }}

        @keyframes fadeIn {{
            from {{ opacity: 0; transform: scale(0.8); }}
            to {{ opacity: 1; transform: scale(1); }}
        }}

        .animated {{
            animation: fadeIn 0.5s ease-out;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🧠 AAP Migration Mind Map - Dependency Analysis</h1>
            <div class="subtitle">
                Generated: {report.analysis_date.strftime('%Y-%m-%d %H:%M:%S')} |
                Source: {report.source_url}
            </div>
            <div class="stats-bar">
                <div class="stat-item">
                    📊 <strong>{report.total_organizations}</strong> Organizations
                </div>
                <div class="stat-item">
                    ✅ <strong>{len(report.independent_orgs)}</strong> Independent
                </div>
                <div class="stat-item">
                    ⚠️ <strong>{len(report.dependent_orgs)}</strong> With Dependencies
                </div>
                <div class="stat-item">
                    📈 <strong>{len(report.migration_phases)}</strong> Migration Phases
                </div>
            </div>
        </div>

        <div class="main-content">
            <div class="sidebar">
                <h2>Organizations</h2>
                <div class="org-list" id="orgList"></div>
            </div>

            <div class="canvas-area">
                <div class="mindmap-canvas" id="mindmapCanvas">
                    <div class="placeholder">
                        <div class="placeholder-icon">🧠</div>
                        <div class="placeholder-text">Select an organization to view its resource mind map</div>
                    </div>
                </div>
                <div class="tooltip" id="tooltip"></div>
            </div>
        </div>
    </div>

    <script>
        // Data
        const orgsData = {orgs_json};
        const edgesData = {edges_json};
        const phasesData = {phases_json};

        let currentOrg = null;

        // Initialize
        function init() {{
            renderOrgList();
        }}

        function renderOrgList() {{
            const orgList = document.getElementById('orgList');
            orgList.innerHTML = '';

            orgsData.forEach(org => {{
                const item = document.createElement('div');
                item.className = `org-item ${{org.has_dependencies ? 'dependent' : 'independent'}}`;
                item.onclick = () => selectOrg(org.name);

                const name = document.createElement('div');
                name.className = 'org-item-name';
                name.textContent = org.name;
                item.appendChild(name);

                const meta = document.createElement('div');
                meta.className = 'org-item-meta';
                meta.textContent = `${{org.total_resources}} resources`;
                item.appendChild(meta);

                if (org.has_dependencies) {{
                    const deps = document.createElement('div');
                    deps.className = 'org-item-deps';
                    deps.textContent = `⚠️ Requires: ${{org.required_before.join(', ')}}`;
                    item.appendChild(deps);
                }}

                orgList.appendChild(item);
            }});
        }}

        function selectOrg(orgName) {{
            currentOrg = orgsData.find(o => o.name === orgName);

            // Update active state
            document.querySelectorAll('.org-item').forEach((item, idx) => {{
                if (orgsData[idx].name === orgName) {{
                    item.classList.add('active');
                }} else {{
                    item.classList.remove('active');
                }}
            }});

            // Render mind map
            renderMindMap(currentOrg);
        }}

        function renderMindMap(org) {{
            const canvas = document.getElementById('mindmapCanvas');
            canvas.innerHTML = '';

            // Get migration phase for this org
            let phaseNum = 0;
            phasesData.forEach(phase => {{
                if (phase.orgs.includes(org.name)) {{
                    phaseNum = phase.phase;
                }}
            }});

            // Create SVG
            const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.setAttribute('class', 'mindmap-svg animated');
            svg.setAttribute('width', '100%');
            svg.setAttribute('height', '100%');

            const width = 1600;
            const height = 1200;
            svg.setAttribute('viewBox', `0 0 ${{width}} ${{height}}`);

            // Center point
            const centerX = width / 2;
            const centerY = height / 2;

            // Draw org node at center
            const orgGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
            orgGroup.setAttribute('class', 'org-node');

            const orgCircle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            orgCircle.setAttribute('cx', centerX);
            orgCircle.setAttribute('cy', centerY);
            orgCircle.setAttribute('r', '80');
            orgCircle.setAttribute('class', 'node-circle');
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
            orgCount.setAttribute('fill', 'white');
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
                link.setAttribute('d', `M ${{centerX}} ${{centerY}} L ${{typeX}} ${{typeY}}`);
                svg.appendChild(link);

                // Draw type node
                const typeGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
                typeGroup.setAttribute('class', 'type-node');

                const typeCircle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                typeCircle.setAttribute('cx', typeX);
                typeCircle.setAttribute('cy', typeY);
                typeCircle.setAttribute('r', '45');
                typeCircle.setAttribute('class', 'node-circle');
                typeCircle.setAttribute('data-type', typeName);
                typeGroup.appendChild(typeCircle);

                const typeText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                typeText.setAttribute('x', typeX);
                typeText.setAttribute('y', typeY - 5);
                typeText.setAttribute('class', 'node-text-small');
                typeText.setAttribute('fill', 'white');
                typeText.textContent = typeName.length > 12 ? typeName.substring(0, 10) + '...' : typeName;
                typeGroup.appendChild(typeText);

                const typeCountText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                typeCountText.setAttribute('x', typeX);
                typeCountText.setAttribute('y', typeY + 8);
                typeCountText.setAttribute('class', 'node-text-small');
                typeCountText.setAttribute('fill', 'white');
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
                    resLink.setAttribute('d', `M ${{typeX}} ${{typeY}} L ${{resX}} ${{resY}}`);
                    svg.appendChild(resLink);

                    // Draw resource node
                    const resGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
                    const resClass = resource.cross_org_deps ? 'resource-node has-cross-org' : 'resource-node';
                    resGroup.setAttribute('class', resClass);

                    const resCircle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                    resCircle.setAttribute('cx', resX);
                    resCircle.setAttribute('cy', resY);
                    resCircle.setAttribute('r', '25');
                    resCircle.setAttribute('class', 'node-circle');
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

            // Add phase indicator
            const phaseDiv = document.createElement('div');
            phaseDiv.className = 'phase-indicator animated';
            phaseDiv.innerHTML = `Migration Phase: <span class="phase-number">${{phaseNum}}</span>`;
            canvas.appendChild(phaseDiv);

            // Add controls
            const controlsDiv = document.createElement('div');
            controlsDiv.className = 'controls animated';
            controlsDiv.innerHTML = `
                <button class="control-btn" onclick="expandAll()">Expand All</button>
                <button class="control-btn" onclick="collapseAll()">Collapse All</button>
                <button class="control-btn" onclick="showDependencies()">Show Dependencies</button>
            `;
            canvas.appendChild(controlsDiv);

            // Add legend
            const legendDiv = document.createElement('div');
            legendDiv.className = 'legend animated';
            legendDiv.innerHTML = `
                <div class="legend-title">Mind Map Legend</div>
                <div class="legend-item">
                    <div class="legend-color org"></div>
                    <span>Organization</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color type"></div>
                    <span>Resource Type</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color resource"></div>
                    <span>Resource</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color cross-org"></div>
                    <span>Cross-Org Dependency</span>
                </div>
            `;
            canvas.appendChild(legendDiv);

            canvas.appendChild(svg);
        }}

        function showTooltip(event, resource, typeName) {{
            const tooltip = document.getElementById('tooltip');

            let html = `<h4>${{resource.name}}</h4>`;
            html += `<div class="tooltip-row"><strong>Type:</strong> ${{typeName}}</div>`;
            html += `<div class="tooltip-row"><strong>ID:</strong> ${{resource.id}}</div>`;

            if (resource.cross_org_deps) {{
                html += `<div class="tooltip-row" style="color: #fca5a5; margin-top: 8px;"><strong>⚠️ Cross-Org Dependencies:</strong></div>`;
                resource.cross_org_deps.forEach(dep => {{
                    html += `<div class="tooltip-row" style="margin-left: 10px;">
                        → ${{dep.resource_type}}: ${{dep.resource_name}} (from ${{dep.org}})
                    </div>`;
                }});
            }}

            tooltip.innerHTML = html;
            tooltip.style.display = 'block';
            tooltip.style.left = (event.pageX + 15) + 'px';
            tooltip.style.top = (event.pageY + 15) + 'px';
        }}

        function hideTooltip() {{
            document.getElementById('tooltip').style.display = 'none';
        }}

        function expandAll() {{
            alert('All resources are already expanded in the mind map!');
        }}

        function collapseAll() {{
            alert('Collapse feature - would hide individual resources, showing only type counts');
        }}

        function showDependencies() {{
            if (!currentOrg || !currentOrg.has_dependencies) {{
                alert('This organization has no cross-org dependencies!');
                return;
            }}

            let msg = `${{currentOrg.name}} depends on:\\n\\n`;
            currentOrg.dependencies.forEach(dep => {{
                msg += `📦 ${{dep.org}}:\\n`;
                dep.resources.forEach(res => {{
                    msg += `  • ${{res.type}}: ${{res.name}}\\n`;
                }});
                msg += '\\n';
            }});

            alert(msg);
        }}

        // Initialize on load
        window.addEventListener('load', init);
    </script>
</body>
</html>'''

    return html
