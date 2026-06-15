"""IAM report generator — HTML summary and JSON detail export.

Produces a self-contained HTML report (Tier 1) with aggregated stats
embedded as JSON, and a separate detail JSON file (Tier 2) with the
full permission matrix.

Security:
  - All user-supplied data HTML-escaped via html.escape()
  - JSON embedded in <script> sanitised against </script> injection
  - No external CDN or resource references — fully air-gapped
  - Output files written with 0o600 permissions
  - Source URLs shown with path stripped (hostname only in report)
  - Tokens never included in any output
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from typing import Any
from urllib.parse import urlparse

from aap_migration.iam.models import (
    CrossOrgShare,
    IAMAuditResult,
    MigrationStats,
    OrgSummary,
    PermissionEntry,
    SystemRoleEntry,
    TeamMembership,
)

_RESOURCE_TYPE_DISPLAY = {
    "organizations": "Organizations",
    "teams": "Teams",
    "credentials": "Credentials",
    "projects": "Projects",
    "inventories": "Inventories",
    "job_templates": "Job Templates",
    "workflow_job_templates": "Workflow Templates",
    "notification_templates": "Notifications",
    "instance_groups": "Instance Groups",
}


def _display_type(rtype: str) -> str:
    return _RESOURCE_TYPE_DISPLAY.get(
        rtype, rtype.replace("_", " ").title()
    )


def _hostname_only(url: str) -> str:
    parsed = urlparse(url)
    return parsed.hostname or url


def _safe_json_embed(data: Any) -> str:
    raw = json.dumps(data, indent=2, default=str)
    return raw.replace("</", "<\\/")


def _write_secure(path: str, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)


# ── Aggregation helpers ───────────────────────────────────────────────


def _build_type_summaries(
    permissions: list[PermissionEntry],
) -> dict[str, dict[str, Any]]:
    by_type: dict[str, dict[str, Any]] = {}
    resources: dict[str, set[int]] = defaultdict(set)

    for p in permissions:
        if p.resource_type not in by_type:
            by_type[p.resource_type] = {
                "display": _display_type(p.resource_type),
                "resource_count": 0,
                "permission_count": 0,
                "roles": {},
            }
        resources[p.resource_type].add(p.resource_id)
        by_type[p.resource_type]["permission_count"] += 1
        by_type[p.resource_type]["roles"][p.role_name] = (
            by_type[p.resource_type]["roles"].get(p.role_name, 0) + 1
        )

    for rtype, res_ids in resources.items():
        by_type[rtype]["resource_count"] = len(res_ids)

    return by_type


def _build_team_summary(
    memberships: list[TeamMembership],
) -> dict[str, dict[str, Any]]:
    by_team: dict[str, dict[str, Any]] = {}

    for m in memberships:
        key = f"{m.team_name} ({m.team_org})"
        if key not in by_team:
            by_team[key] = {
                "team_name": m.team_name,
                "org": m.team_org,
                "members": [],
                "migrated": 0,
                "failed": 0,
            }
        by_team[key]["members"].append(
            {"username": m.username, "status": m.status}
        )
        if m.status == "migrated":
            by_team[key]["migrated"] += 1
        elif m.status == "failed":
            by_team[key]["failed"] += 1

    return by_team


# ── JSON export / import ─────────────────────────────────────────────


def export_iam_json(result: IAMAuditResult, output_path: str) -> None:
    data = result.to_dict()
    data["metadata"]["generated"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    data["metadata"]["source_host"] = _hostname_only(result.source_url)
    _write_secure(output_path, json.dumps(data, indent=2, default=str))


def load_audit_result_from_json(json_path: str) -> IAMAuditResult:
    with open(json_path) as fh:
        data = json.load(fh)

    meta = data.get("metadata", {})
    stats_raw = data.get("statistics", {})
    stats = MigrationStats(**{
        k: v for k, v in stats_raw.items() if k in MigrationStats.__dataclass_fields__
    })

    permissions = [PermissionEntry(**p) for p in data.get("permissions", [])]
    memberships = [
        TeamMembership(**m) for m in data.get("team_memberships", [])
    ]
    system_roles = [
        SystemRoleEntry(**r) for r in data.get("system_roles", [])
    ]
    cross_org_shares = [
        CrossOrgShare(**c) for c in data.get("cross_org_shares", [])
    ]

    org_summaries_raw = data.get("org_summaries", {})
    org_summaries = {}
    for name, raw in org_summaries_raw.items():
        filtered = {
            k: v for k, v in raw.items()
            if k in OrgSummary.__dataclass_fields__ and k != "success_rate"
        }
        org_summaries[name] = OrgSummary(**filtered)

    return IAMAuditResult(
        mode=meta.get("mode", "audit"),
        source_url=meta.get("source_url", ""),
        permissions=permissions,
        team_memberships=memberships,
        system_roles=system_roles,
        cross_org_shares=cross_org_shares,
        org_summaries=org_summaries,
        stats=stats,
    )


# ── HTML report ──────────────────────────────────────────────────────


def generate_iam_html_report(result: IAMAuditResult) -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    source_host = escape(_hostname_only(result.source_url))
    mode_label = {
        "audit": "Audit",
        "migrate": "Migration",
        "dry_run": "Dry-Run",
    }.get(result.mode, result.mode.title())

    report_data = {
        "metadata": {
            "generated": generated,
            "mode": result.mode,
            "mode_label": mode_label,
            "source_host": source_host,
        },
        "stats": result.stats.to_dict(),
        "org_summaries": {
            k: v.to_dict() for k, v in result.org_summaries.items()
        },
        "type_summaries": _build_type_summaries(result.permissions),
        "cross_org_shares": [c.to_dict() for c in result.cross_org_shares],
        "system_roles": [r.to_dict() for r in result.system_roles],
        "team_summary": _build_team_summary(result.team_memberships),
        "failures": [
            p.to_dict()
            for p in result.permissions
            if p.status == "failed"
        ],
        "membership_failures": [
            m.to_dict()
            for m in result.team_memberships
            if m.status == "failed"
        ],
    }

    json_block = _safe_json_embed(report_data)

    is_migrate = result.mode in ("migrate", "dry_run")
    failures_tab = (
        '<button class="tab" data-tab="failures">Failures</button>'
        if is_migrate
        else ""
    )
    failures_content = (
        '<div class="content hidden" id="failuresContent"></div>'
        if is_migrate
        else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AAP IAM {escape(mode_label)} Report</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);padding:20px;min-height:100vh}}
.container{{max-width:1600px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.2);overflow:hidden}}
.header{{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;padding:30px;text-align:center}}
.header h1{{font-size:2em;margin-bottom:10px}}
.header .metadata{{opacity:.9;font-size:.9em}}
.tabs{{display:flex;background:#f8f9fa;border-bottom:3px solid #e9ecef;overflow-x:auto}}
.tab{{padding:15px 25px;cursor:pointer;border:none;background:transparent;font-size:.95em;font-weight:600;color:#6c757d;transition:all .3s;border-bottom:3px solid transparent;margin-bottom:-3px;white-space:nowrap}}
.tab:hover{{background:#e9ecef;color:#495057}}
.tab.active{{color:#667eea;border-bottom-color:#667eea;background:#fff}}
.stats{{padding:20px 30px;display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:15px}}
.stat-card{{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;padding:20px;border-radius:8px;text-align:center}}
.stat-card.success{{background:linear-gradient(135deg,#11998e 0%,#38ef7d 100%)}}
.stat-card.warning{{background:linear-gradient(135deg,#f7971e 0%,#ffd200 100%);color:#333}}
.stat-card.danger{{background:linear-gradient(135deg,#f093fb 0%,#f5576c 100%)}}
.stat-card .value{{font-size:2.2em;font-weight:bold;margin-bottom:5px}}
.stat-card .label{{font-size:.85em;opacity:.9}}
.content{{padding:30px;min-height:300px}}
.content.hidden{{display:none}}
table{{width:100%;border-collapse:collapse;margin-top:15px;font-size:.9em}}
th{{background:#667eea;color:#fff;padding:12px;text-align:left;font-weight:600;position:sticky;top:0}}
td{{padding:10px 12px;border-bottom:1px solid #e9ecef}}
tr:hover{{background:#f8f9fa}}
.badge{{padding:3px 8px;border-radius:4px;font-size:.8em;font-weight:600}}
.badge-audit{{background:#e3f2fd;color:#1565c0}}
.badge-migrated{{background:#c8e6c9;color:#2e7d32}}
.badge-failed{{background:#ffcdd2;color:#c62828}}
.badge-dry_run{{background:#fff3e0;color:#e65100}}
.success-rate{{font-weight:600;padding:3px 8px;border-radius:4px}}
.success-rate.high{{background:#d4edda;color:#155724}}
.success-rate.medium{{background:#fff3cd;color:#856404}}
.success-rate.low{{background:#f8d7da;color:#721c24}}
.section-title{{font-size:1.1em;font-weight:600;color:#495057;padding:10px 0;border-bottom:2px solid #e9ecef;margin-bottom:15px;margin-top:25px}}
.pagination{{display:flex;justify-content:center;align-items:center;gap:10px;padding:20px;margin-top:20px}}
.pagination.hidden{{display:none}}
.pagination button{{padding:8px 16px;border:2px solid #667eea;background:#fff;color:#667eea;border-radius:6px;cursor:pointer;font-weight:600;transition:all .3s}}
.pagination button:hover:not(:disabled){{background:#667eea;color:#fff}}
.pagination button:disabled{{opacity:.3;cursor:not-allowed}}
.pagination .page-info{{padding:0 15px;font-weight:600;color:#495057}}
.no-data{{text-align:center;padding:60px 20px;color:#6c757d}}
.error-cell{{max-width:400px;word-wrap:break-word;font-size:.85em;color:#495057}}
.search-box{{padding:10px 15px;border:2px solid #dee2e6;border-radius:6px;font-size:14px;min-width:250px;margin-bottom:15px}}
.search-box:focus{{outline:none;border-color:#667eea;box-shadow:0 0 0 3px rgba(102,126,234,.1)}}
</style>
</head>
<body>
<div class="container">
<div class="header">
<h1>AAP IAM {escape(mode_label)} Report</h1>
<div class="metadata">Generated: {escape(generated)} | Source: {source_host}</div>
</div>
<div class="tabs">
<button class="tab active" data-tab="dashboard">Dashboard</button>
<button class="tab" data-tab="byOrg">By Organization</button>
<button class="tab" data-tab="byType">By Resource Type</button>
<button class="tab" data-tab="crossOrg">Cross-Org Sharing</button>
<button class="tab" data-tab="teams">Team Memberships</button>
<button class="tab" data-tab="sysRoles">System Roles</button>
{failures_tab}
</div>
<div class="stats" id="statsContainer"></div>
<div class="content" id="dashboardContent"></div>
<div class="content hidden" id="byOrgContent"></div>
<div class="content hidden" id="byTypeContent"></div>
<div class="content hidden" id="crossOrgContent"></div>
<div class="content hidden" id="teamsContent"></div>
<div class="content hidden" id="sysRolesContent"></div>
{failures_content}
<div class="pagination hidden" id="paginationContainer">
<button id="prevPage">Previous</button>
<span class="page-info" id="pageInfo">Page 1 of 1</span>
<button id="nextPage">Next</button>
</div>
</div>
<script>
const D={json_block};
const IS_MIGRATE=D.metadata.mode==='migrate'||D.metadata.mode==='dry_run';
let curTab='dashboard',curPage=1,filtered=[];
const PER_PAGE=100;
function esc(t){{const d=document.createElement('div');d.textContent=t;return d.innerHTML}}
function init(){{
renderDashboard();
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>switchTab(t.dataset.tab)));
document.getElementById('prevPage').addEventListener('click',()=>{{if(curPage>1){{curPage--;renderPage()}}}});
document.getElementById('nextPage').addEventListener('click',()=>{{const tp=Math.ceil(filtered.length/PER_PAGE);if(curPage<tp){{curPage++;renderPage()}}}});
}}
function switchTab(tab){{
curTab=tab;curPage=1;
document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.dataset.tab===tab));
document.querySelectorAll('.content').forEach(c=>c.classList.add('hidden'));
document.getElementById('paginationContainer').classList.add('hidden');
if(tab==='dashboard'){{document.getElementById('dashboardContent').classList.remove('hidden');renderDashboard()}}
else if(tab==='byOrg'){{document.getElementById('byOrgContent').classList.remove('hidden');renderByOrg()}}
else if(tab==='byType'){{document.getElementById('byTypeContent').classList.remove('hidden');renderByType()}}
else if(tab==='crossOrg'){{document.getElementById('crossOrgContent').classList.remove('hidden');renderCrossOrg()}}
else if(tab==='teams'){{document.getElementById('teamsContent').classList.remove('hidden');renderTeams()}}
else if(tab==='sysRoles'){{document.getElementById('sysRolesContent').classList.remove('hidden');renderSysRoles()}}
else if(tab==='failures'){{document.getElementById('failuresContent').classList.remove('hidden');renderFailures()}}
}}
function renderDashboard(){{
const s=D.stats;
const uniqueUsers=new Set();const uniqueTeams=new Set();
D.stats._raw_not_available=true;
let cards=`
<div class="stat-card"><div class="value">${{s.resources_scanned}}</div><div class="label">Resources Scanned</div></div>
<div class="stat-card"><div class="value">${{s.permissions_found}}</div><div class="label">Permissions Found</div></div>
<div class="stat-card"><div class="value">${{s.team_memberships_found}}</div><div class="label">Team Memberships</div></div>
<div class="stat-card"><div class="value">${{s.system_roles_found}}</div><div class="label">System Roles</div></div>
<div class="stat-card warning"><div class="value">${{s.cross_org_shares}}</div><div class="label">Cross-Org Shares</div></div>`;
if(s.permissions_deduplicated){{cards+=`<div class="stat-card"><div class="value">${{s.permissions_deduplicated}}</div><div class="label">Deduplicated</div></div>`}}
if(IS_MIGRATE){{
const rate=s.permissions_found>0?Math.round((s.permissions_migrated/s.permissions_found)*100):0;
cards+=`
<div class="stat-card success"><div class="value">${{s.permissions_migrated}}</div><div class="label">Migrated</div></div>
<div class="stat-card danger"><div class="value">${{s.permissions_failed}}</div><div class="label">Failed</div></div>
<div class="stat-card"><div class="value">${{rate}}%</div><div class="label">Success Rate</div></div>`;
}}
document.getElementById('statsContainer').innerHTML=cards;
const orgs=Object.entries(D.org_summaries).sort((a,b)=>b[1].permissions_total-a[1].permissions_total);
let h='<div class="section-title">Top Organizations by Permission Count</div>';
h+='<table><thead><tr><th>Organization</th><th>Resources</th><th>Permissions</th><th>Team Members</th><th>Cross-Org</th>';
if(IS_MIGRATE)h+='<th>Success Rate</th>';
h+='</tr></thead><tbody>';
orgs.slice(0,20).forEach(([name,s])=>{{
let rateHtml='';
if(IS_MIGRATE){{
const r=s.success_rate||0;const cls=r>=80?'high':r>=50?'medium':'low';
rateHtml=`<td><span class="success-rate ${{cls}}">${{r}}%</span></td>`;
}}
h+=`<tr><td><strong>${{esc(name)}}</strong></td><td>${{s.resources_scanned}}</td><td>${{s.permissions_total}}</td><td>${{s.team_memberships_total}}</td><td>${{s.cross_org_shares}}</td>${{rateHtml}}</tr>`;
}});
h+='</tbody></table>';
if(orgs.length>20)h+=`<p style="color:#6c757d;margin-top:10px">Showing top 20 of ${{orgs.length}} organizations. See By Organization tab for full list.</p>`;
document.getElementById('dashboardContent').innerHTML=h;
}}
function renderByOrg(){{
const orgs=Object.entries(D.org_summaries).sort((a,b)=>b[1].permissions_total-a[1].permissions_total);
document.getElementById('statsContainer').innerHTML='';
let h='<input class="search-box" id="orgSearch" placeholder="Search organizations..." oninput="filterOrgs()">';
h+='<table><thead><tr><th>Organization</th><th>Resources</th><th>Permissions</th><th>By Type</th><th>By Role</th><th>Team Members</th><th>Cross-Org</th>';
if(IS_MIGRATE)h+='<th>Success Rate</th>';
h+='</tr></thead><tbody id="orgTableBody">';
orgs.forEach(([name,s])=>{{
const types=Object.entries(s.permissions_by_type||{{}}).map(([t,c])=>`${{t}}:${{c}}`).join(', ');
const roles=Object.entries(s.permissions_by_role||{{}}).map(([r,c])=>`${{r}}:${{c}}`).join(', ');
let rateHtml='';
if(IS_MIGRATE){{
const r=s.success_rate||0;const cls=r>=80?'high':r>=50?'medium':'low';
rateHtml=`<td><span class="success-rate ${{cls}}">${{r}}%</span></td>`;
}}
h+=`<tr data-org="${{esc(name.toLowerCase())}}"><td><strong>${{esc(name)}}</strong></td><td>${{s.resources_scanned}}</td><td>${{s.permissions_total}}</td><td style="font-size:.8em">${{esc(types)}}</td><td style="font-size:.8em">${{esc(roles)}}</td><td>${{s.team_memberships_total}}</td><td>${{s.cross_org_shares}}</td>${{rateHtml}}</tr>`;
}});
h+='</tbody></table>';
document.getElementById('byOrgContent').innerHTML=h;
}}
function filterOrgs(){{
const q=document.getElementById('orgSearch').value.toLowerCase();
document.querySelectorAll('#orgTableBody tr').forEach(r=>{{
r.style.display=r.dataset.org.includes(q)?'':'none';
}});
}}
function renderByType(){{
document.getElementById('statsContainer').innerHTML='';
const types=Object.entries(D.type_summaries).sort((a,b)=>b[1].permission_count-a[1].permission_count);
let h='<table><thead><tr><th>Resource Type</th><th>Resources</th><th>Permissions</th><th>Role Distribution</th></tr></thead><tbody>';
types.forEach(([key,t])=>{{
const roles=Object.entries(t.roles||{{}}).sort((a,b)=>b[1]-a[1]).map(([r,c])=>`${{r}}: ${{c}}`).join(', ');
h+=`<tr><td><strong>${{esc(t.display||key)}}</strong></td><td>${{t.resource_count}}</td><td>${{t.permission_count}}</td><td style="font-size:.85em">${{esc(roles)}}</td></tr>`;
}});
h+='</tbody></table>';
document.getElementById('byTypeContent').innerHTML=h;
}}
function renderCrossOrg(){{
document.getElementById('statsContainer').innerHTML='';
const shares=D.cross_org_shares;
if(!shares.length){{document.getElementById('crossOrgContent').innerHTML='<div class="no-data"><p>No cross-organization sharing detected.</p></div>';return}}
let h='<table><thead><tr><th>Resource</th><th>Type</th><th>Owner Org</th><th>Shared With</th><th>Permissions</th></tr></thead><tbody>';
shares.forEach(s=>{{
h+=`<tr><td><strong>${{esc(s.resource_name)}}</strong></td><td>${{esc(s.resource_type)}}</td><td>${{esc(s.resource_org)}}</td><td>${{esc(s.shared_with_orgs.join(', '))}}</td><td>${{s.permission_count}}</td></tr>`;
}});
h+='</tbody></table>';
document.getElementById('crossOrgContent').innerHTML=h;
}}
function renderTeams(){{
document.getElementById('statsContainer').innerHTML='';
const teams=Object.entries(D.team_summary).sort((a,b)=>b[1].members.length-a[1].members.length);
if(!teams.length){{document.getElementById('teamsContent').innerHTML='<div class="no-data"><p>No team memberships found.</p></div>';return}}
let h='<input class="search-box" id="teamSearch" placeholder="Search teams..." oninput="filterTeams()">';
h+='<table><thead><tr><th>Team</th><th>Organization</th><th>Members</th><th>Member List</th>';
if(IS_MIGRATE)h+='<th>Migrated</th><th>Failed</th>';
h+='</tr></thead><tbody id="teamTableBody">';
teams.forEach(([key,t])=>{{
const memberNames=t.members.map(m=>m.username).join(', ');
let extra='';
if(IS_MIGRATE)extra=`<td>${{t.migrated}}</td><td>${{t.failed}}</td>`;
h+=`<tr data-team="${{esc(key.toLowerCase())}}"><td><strong>${{esc(t.team_name)}}</strong></td><td>${{esc(t.org)}}</td><td>${{t.members.length}}</td><td style="font-size:.85em">${{esc(memberNames)}}</td>${{extra}}</tr>`;
}});
h+='</tbody></table>';
document.getElementById('teamsContent').innerHTML=h;
}}
function filterTeams(){{
const q=document.getElementById('teamSearch').value.toLowerCase();
document.querySelectorAll('#teamTableBody tr').forEach(r=>{{
r.style.display=r.dataset.team.includes(q)?'':'none';
}});
}}
function renderSysRoles(){{
document.getElementById('statsContainer').innerHTML='';
const roles=D.system_roles;
if(!roles.length){{document.getElementById('sysRolesContent').innerHTML='<div class="no-data"><p>No system-level roles detected.</p></div>';return}}
let h='<table><thead><tr><th>Username</th><th>Role</th></tr></thead><tbody>';
roles.forEach(r=>{{
const label=r.flag==='is_superuser'?'System Administrator':'System Auditor';
h+=`<tr><td><strong>${{esc(r.username)}}</strong></td><td><span class="badge badge-audit">${{esc(label)}}</span></td></tr>`;
}});
h+='</tbody></table>';
document.getElementById('sysRolesContent').innerHTML=h;
}}
function renderFailures(){{
const perms=D.failures||[];
const mems=D.membership_failures||[];
const total=perms.length+mems.length;
document.getElementById('statsContainer').innerHTML=`<div class="stat-card danger"><div class="value">${{total}}</div><div class="label">Total Failures</div></div>`;
if(!total){{document.getElementById('failuresContent').innerHTML='<div class="no-data"><p>No failures recorded.</p></div>';return}}
filtered=perms;curPage=1;
let h='';
if(mems.length){{
h+='<div class="section-title">Team Membership Failures</div>';
h+='<table><thead><tr><th>Team</th><th>Organization</th><th>Username</th><th>Error</th></tr></thead><tbody>';
mems.forEach(m=>{{h+=`<tr><td>${{esc(m.team_name)}}</td><td>${{esc(m.team_org)}}</td><td>${{esc(m.username)}}</td><td class="error-cell">${{esc(m.error)}}</td></tr>`}});
h+='</tbody></table>';
}}
if(perms.length){{
h+='<div class="section-title">Permission Failures</div>';
h+='<div id="failureTableContainer"></div>';
}}
document.getElementById('failuresContent').innerHTML=h;
if(perms.length)renderPage();
}}
function renderPage(){{
const start=(curPage-1)*PER_PAGE;const end=start+PER_PAGE;
const page=filtered.slice(start,end);
if(!page.length)return;
let h='<table><thead><tr><th>Resource</th><th>Type</th><th>Role</th><th>Principal</th><th>Error</th></tr></thead><tbody>';
page.forEach(p=>{{
h+=`<tr><td>${{esc(p.resource_name)}}</td><td>${{esc(p.resource_type)}}</td><td>${{esc(p.role_name)}}</td><td>${{esc(p.principal_name)}} (${{esc(p.principal_type)}})</td><td class="error-cell">${{esc(p.error)}}</td></tr>`;
}});
h+='</tbody></table>';
const container=document.getElementById('failureTableContainer');
if(container)container.innerHTML=h;
const totalPages=Math.ceil(filtered.length/PER_PAGE);
if(totalPages>1){{
document.getElementById('paginationContainer').classList.remove('hidden');
document.getElementById('pageInfo').textContent=`Page ${{curPage}} of ${{totalPages}} (${{start+1}}-${{Math.min(end,filtered.length)}} of ${{filtered.length}})`;
document.getElementById('prevPage').disabled=curPage===1;
document.getElementById('nextPage').disabled=curPage===totalPages;
}}else{{
document.getElementById('paginationContainer').classList.add('hidden');
}}
}}
init();
</script>
</body>
</html>"""

    return html


def write_iam_report(
    result: IAMAuditResult,
    output_dir: str,
    json_filename: str = "iam_report.json",
    html_filename: str = "iam_report.html",
) -> tuple[str, str]:
    os.makedirs(output_dir, mode=0o700, exist_ok=True)

    json_path = os.path.join(output_dir, json_filename)
    export_iam_json(result, json_path)

    html_path = os.path.join(output_dir, html_filename)
    html_content = generate_iam_html_report(result)
    _write_secure(html_path, html_content)

    return json_path, html_path
