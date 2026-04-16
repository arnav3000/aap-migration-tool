"""Self-contained HTML report generator for dependency analysis.

All CSS, JavaScript, and assets are embedded inline for air-gapped environments.
No external dependencies, CDNs, or internet connection required.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aap_migration.analysis.dependency_analyzer import GlobalDependencyReport


def generate_html_report(report: GlobalDependencyReport) -> str:
    """Generate self-contained HTML dependency report.

    Args:
        report: Global dependency analysis report

    Returns:
        Complete HTML string with embedded CSS/JS
    """
    # Build nodes and edges for graph
    nodes_data = []
    edges_data = []

    for org_name, org_report in report.org_reports.items():
        # Node data
        node_type = "independent" if not org_report.has_cross_org_deps else "dependent"
        nodes_data.append({
            "id": org_name,
            "label": org_name,
            "resources": org_report.resource_count,
            "type": node_type,
            "dependencies": org_report.required_migrations_before
        })

        # Edge data
        for dep_org in org_report.required_migrations_before:
            edges_data.append({
                "from": dep_org,
                "to": org_name
            })

    # Build phase data
    phases_html = []
    for phase in report.migration_phases:
        phase_num = phase["phase"]
        orgs = phase["orgs"]

        org_cards = []
        for org in orgs:
            org_report = report.org_reports[org]
            card_class = "org-card independent" if not org_report.has_cross_org_deps else "org-card dependent"
            org_cards.append(f'''
                <div class="{card_class}" onclick="highlightOrg('{org}')">
                    <div class="org-name">{org}</div>
                    <div class="org-resources">{org_report.resource_count} resources</div>
                </div>
            ''')

        phases_html.append(f'''
            <div class="phase">
                <div class="phase-header">Phase {phase_num}</div>
                <div class="phase-orgs">
                    {''.join(org_cards)}
                </div>
            </div>
        ''')

    # Build detailed table
    details_rows = []
    for org_name in sorted(report.org_reports.keys()):
        org_report = report.org_reports[org_name]

        if org_report.has_cross_org_deps:
            deps = ', '.join(org_report.required_migrations_before)
            status_icon = "⚠️"
            status_text = "Has Dependencies"
        else:
            deps = "-"
            status_icon = "✓"
            status_text = "Independent"

        details_rows.append(f'''
            <tr class="{'dependent-row' if org_report.has_cross_org_deps else ''}">
                <td>{status_icon}</td>
                <td>{org_name}</td>
                <td class="number">{org_report.resource_count}</td>
                <td>{status_text}</td>
                <td>{deps}</td>
            </tr>
        ''')

    # Generate complete HTML
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AAP Migration Dependency Analysis Report</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            min-height: 100vh;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }}

        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px;
            text-align: center;
        }}

        .header h1 {{
            font-size: 2.5em;
            margin-bottom: 10px;
            font-weight: 700;
        }}

        .header .subtitle {{
            font-size: 1.1em;
            opacity: 0.9;
        }}

        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            padding: 30px 40px;
            background: #f8f9fa;
            border-bottom: 2px solid #e9ecef;
        }}

        .stat-card {{
            background: white;
            padding: 20px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            text-align: center;
            transition: transform 0.2s;
        }}

        .stat-card:hover {{
            transform: translateY(-5px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        }}

        .stat-value {{
            font-size: 2.5em;
            font-weight: 700;
            color: #667eea;
        }}

        .stat-label {{
            font-size: 0.9em;
            color: #6c757d;
            margin-top: 5px;
        }}

        .stat-card.warning .stat-value {{
            color: #f59e0b;
        }}

        .stat-card.success .stat-value {{
            color: #10b981;
        }}

        .section {{
            padding: 40px;
        }}

        .section-title {{
            font-size: 1.8em;
            font-weight: 700;
            margin-bottom: 25px;
            color: #1f2937;
            border-bottom: 3px solid #667eea;
            padding-bottom: 10px;
        }}

        .graph-container {{
            background: #f8f9fa;
            border-radius: 12px;
            padding: 30px;
            margin-bottom: 30px;
            min-height: 400px;
            position: relative;
        }}

        .graph-svg {{
            width: 100%;
            height: 500px;
        }}

        .node {{
            cursor: pointer;
            transition: all 0.3s;
        }}

        .node:hover {{
            filter: brightness(1.1);
        }}

        .node.independent circle {{
            fill: #10b981;
            stroke: #059669;
        }}

        .node.dependent circle {{
            fill: #f59e0b;
            stroke: #d97706;
        }}

        .node circle {{
            stroke-width: 3;
        }}

        .node.highlighted circle {{
            stroke: #dc2626;
            stroke-width: 5;
        }}

        .node text {{
            font-size: 12px;
            font-weight: 600;
            fill: #1f2937;
            pointer-events: none;
        }}

        .edge {{
            stroke: #9ca3af;
            stroke-width: 2;
            fill: none;
            marker-end: url(#arrowhead);
        }}

        .edge.highlighted {{
            stroke: #dc2626;
            stroke-width: 3;
        }}

        .phases {{
            display: flex;
            flex-direction: column;
            gap: 20px;
        }}

        .phase {{
            background: white;
            border-radius: 12px;
            border: 2px solid #e5e7eb;
            overflow: hidden;
        }}

        .phase-header {{
            background: #667eea;
            color: white;
            padding: 15px 20px;
            font-weight: 700;
            font-size: 1.2em;
        }}

        .phase-orgs {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 15px;
            padding: 20px;
        }}

        .org-card {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
            border: 2px solid #e5e7eb;
            cursor: pointer;
            transition: all 0.2s;
        }}

        .org-card:hover {{
            transform: translateY(-3px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        }}

        .org-card.independent {{
            border-color: #10b981;
            background: #f0fdf4;
        }}

        .org-card.dependent {{
            border-color: #f59e0b;
            background: #fffbeb;
        }}

        .org-name {{
            font-weight: 600;
            color: #1f2937;
            margin-bottom: 5px;
        }}

        .org-resources {{
            font-size: 0.85em;
            color: #6c757d;
        }}

        .details-table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .details-table th {{
            background: #667eea;
            color: white;
            padding: 15px;
            text-align: left;
            font-weight: 600;
            cursor: pointer;
            user-select: none;
        }}

        .details-table th:hover {{
            background: #5568d3;
        }}

        .details-table td {{
            padding: 12px 15px;
            border-bottom: 1px solid #e5e7eb;
        }}

        .details-table tr:hover {{
            background: #f8f9fa;
        }}

        .dependent-row {{
            background: #fffbeb;
        }}

        .number {{
            text-align: center;
        }}

        .legend {{
            display: flex;
            gap: 20px;
            justify-content: center;
            margin-top: 20px;
            padding: 15px;
            background: white;
            border-radius: 8px;
        }}

        .legend-item {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .legend-color {{
            width: 20px;
            height: 20px;
            border-radius: 50%;
            border: 2px solid;
        }}

        .legend-color.independent {{
            background: #10b981;
            border-color: #059669;
        }}

        .legend-color.dependent {{
            background: #f59e0b;
            border-color: #d97706;
        }}

        @media print {{
            body {{
                background: white;
            }}
            .container {{
                box-shadow: none;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 AAP Migration Dependency Analysis</h1>
            <div class="subtitle">Generated: {report.analysis_date.strftime('%Y-%m-%d %H:%M:%S')}</div>
            <div class="subtitle">Source: {report.source_url}</div>
        </div>

        <div class="stats">
            <div class="stat-card">
                <div class="stat-value">{report.total_organizations}</div>
                <div class="stat-label">Total Organizations</div>
            </div>
            <div class="stat-card success">
                <div class="stat-value">{len(report.independent_orgs)}</div>
                <div class="stat-label">Independent</div>
            </div>
            <div class="stat-card warning">
                <div class="stat-value">{len(report.dependent_orgs)}</div>
                <div class="stat-label">With Dependencies</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{len(report.migration_phases)}</div>
                <div class="stat-label">Migration Phases</div>
            </div>
        </div>

        <div class="section">
            <div class="section-title">Dependency Graph</div>
            <div class="graph-container">
                <svg class="graph-svg" id="dependencyGraph"></svg>
                <div class="legend">
                    <div class="legend-item">
                        <div class="legend-color independent"></div>
                        <span>Independent (no dependencies)</span>
                    </div>
                    <div class="legend-item">
                        <div class="legend-color dependent"></div>
                        <span>Has dependencies</span>
                    </div>
                </div>
            </div>
        </div>

        <div class="section">
            <div class="section-title">Migration Phases</div>
            <div class="phases">
                {''.join(phases_html)}
            </div>
        </div>

        <div class="section">
            <div class="section-title">Detailed Organization Analysis</div>
            <table class="details-table">
                <thead>
                    <tr>
                        <th onclick="sortTable(0)">Status</th>
                        <th onclick="sortTable(1)">Organization</th>
                        <th onclick="sortTable(2)">Resources</th>
                        <th onclick="sortTable(3)">Migration Status</th>
                        <th onclick="sortTable(4)">Dependencies</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(details_rows)}
                </tbody>
            </table>
        </div>
    </div>

    <script>
        // Graph data
        const nodes = {repr(nodes_data)};
        const edges = {repr(edges_data)};

        // Draw dependency graph
        function drawGraph() {{
            const svg = document.getElementById('dependencyGraph');
            const width = svg.clientWidth;
            const height = svg.clientHeight;

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
            polygon.setAttribute('fill', '#9ca3af');
            marker.appendChild(polygon);
            defs.appendChild(marker);
            svg.appendChild(defs);

            // Calculate positions (force-directed layout simulation)
            const nodePositions = {{}};
            const numNodes = nodes.length;
            const centerX = width / 2;
            const centerY = height / 2;
            const radius = Math.min(width, height) * 0.35;

            // Separate independent and dependent nodes
            const independentNodes = nodes.filter(n => n.type === 'independent');
            const dependentNodes = nodes.filter(n => n.type === 'dependent');

            // Position independent nodes in outer circle
            independentNodes.forEach((node, i) => {{
                const angle = (i / independentNodes.length) * 2 * Math.PI;
                nodePositions[node.id] = {{
                    x: centerX + radius * Math.cos(angle),
                    y: centerY + radius * Math.sin(angle)
                }};
            }});

            // Position dependent nodes in inner circle
            dependentNodes.forEach((node, i) => {{
                const angle = (i / Math.max(dependentNodes.length, 1)) * 2 * Math.PI;
                nodePositions[node.id] = {{
                    x: centerX + (radius * 0.5) * Math.cos(angle),
                    y: centerY + (radius * 0.5) * Math.sin(angle)
                }};
            }});

            // Draw edges
            edges.forEach(edge => {{
                const from = nodePositions[edge.from];
                const to = nodePositions[edge.to];
                if (from && to) {{
                    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                    path.setAttribute('class', 'edge');
                    path.setAttribute('d', `M ${{from.x}} ${{from.y}} L ${{to.x}} ${{to.y}}`);
                    path.setAttribute('data-from', edge.from);
                    path.setAttribute('data-to', edge.to);
                    svg.appendChild(path);
                }}
            }});

            // Draw nodes
            nodes.forEach(node => {{
                const pos = nodePositions[node.id];
                if (!pos) return;

                const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
                g.setAttribute('class', `node ${{node.type}}`);
                g.setAttribute('data-org', node.id);
                g.setAttribute('onclick', `highlightOrg('${{node.id}}')`);

                // Node circle (size based on resource count)
                const nodeRadius = Math.max(20, Math.min(40, 15 + node.resources / 2));
                const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circle.setAttribute('cx', pos.x);
                circle.setAttribute('cy', pos.y);
                circle.setAttribute('r', nodeRadius);
                g.appendChild(circle);

                // Node label
                const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                text.setAttribute('x', pos.x);
                text.setAttribute('y', pos.y + nodeRadius + 15);
                text.setAttribute('text-anchor', 'middle');
                text.textContent = node.id.length > 15 ? node.id.substring(0, 13) + '...' : node.id;
                g.appendChild(text);

                // Resource count
                const count = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                count.setAttribute('x', pos.x);
                count.setAttribute('y', pos.y + 5);
                count.setAttribute('text-anchor', 'middle');
                count.setAttribute('fill', 'white');
                count.setAttribute('font-weight', 'bold');
                count.textContent = node.resources;
                g.appendChild(count);

                svg.appendChild(g);
            }});
        }}

        // Highlight organization and its dependencies
        function highlightOrg(orgName) {{
            // Reset all highlights
            document.querySelectorAll('.node').forEach(n => n.classList.remove('highlighted'));
            document.querySelectorAll('.edge').forEach(e => e.classList.remove('highlighted'));

            // Highlight selected node
            const node = document.querySelector(`.node[data-org="${{orgName}}"]`);
            if (node) {{
                node.classList.add('highlighted');

                // Highlight incoming edges (dependencies)
                document.querySelectorAll(`.edge[data-to="${{orgName}}"]`).forEach(e => {{
                    e.classList.add('highlighted');
                    const from = e.getAttribute('data-from');
                    const fromNode = document.querySelector(`.node[data-org="${{from}}"]`);
                    if (fromNode) fromNode.classList.add('highlighted');
                }});

                // Highlight outgoing edges (dependents)
                document.querySelectorAll(`.edge[data-from="${{orgName}}"]`).forEach(e => {{
                    e.classList.add('highlighted');
                    const to = e.getAttribute('data-to');
                    const toNode = document.querySelector(`.node[data-org="${{to}}"]`);
                    if (toNode) toNode.classList.add('highlighted');
                }});
            }}
        }}

        // Sort table
        function sortTable(columnIndex) {{
            const table = document.querySelector('.details-table tbody');
            const rows = Array.from(table.querySelectorAll('tr'));
            const isNumeric = columnIndex === 2;

            rows.sort((a, b) => {{
                const aValue = a.cells[columnIndex].textContent.trim();
                const bValue = b.cells[columnIndex].textContent.trim();

                if (isNumeric) {{
                    return parseInt(aValue) - parseInt(bValue);
                }}
                return aValue.localeCompare(bValue);
            }});

            rows.forEach(row => table.appendChild(row));
        }}

        // Initialize
        window.addEventListener('load', () => {{
            drawGraph();
        }});

        window.addEventListener('resize', () => {{
            const svg = document.getElementById('dependencyGraph');
            svg.innerHTML = '';
            drawGraph();
        }});
    </script>
</body>
</html>'''

    return html
