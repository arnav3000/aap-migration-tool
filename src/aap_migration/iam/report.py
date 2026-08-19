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
        "permissions": [p.to_dict() for p in result.permissions],
        "cross_org_shares": [c.to_dict() for c in result.cross_org_shares],
        "system_roles": [r.to_dict() for r in result.system_roles],
        "team_summary": _build_team_summary(result.team_memberships),
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
    failures_div = (
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
.stats{{padding:20px 30px;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.stat-card{{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;padding:18px;border-radius:8px;text-align:center}}
.stat-card.success{{background:linear-gradient(135deg,#11998e 0%,#38ef7d 100%)}}
.stat-card.warning{{background:linear-gradient(135deg,#f7971e 0%,#ffd200 100%);color:#333}}
.stat-card.danger{{background:linear-gradient(135deg,#f093fb 0%,#f5576c 100%)}}
.stat-card .value{{font-size:2em;font-weight:bold;margin-bottom:4px}}
.stat-card .label{{font-size:.82em;opacity:.9}}
.content{{padding:30px;min-height:300px}}
.content.hidden{{display:none}}
table{{width:100%;border-collapse:collapse;margin-top:12px;font-size:.88em}}
th{{background:#667eea;color:#fff;padding:10px 12px;text-align:left;font-weight:600;position:sticky;top:0;z-index:1}}
td{{padding:9px 12px;border-bottom:1px solid #e9ecef}}
tr:hover{{background:#f8f9fa}}
tr.clickable{{cursor:pointer}}
tr.clickable:hover{{background:#e8ecff}}
.badge{{display:inline-block;padding:3px 8px;border-radius:4px;font-size:.78em;font-weight:600}}
.badge-audit{{background:#e3f2fd;color:#1565c0}}
.badge-migrated{{background:#c8e6c9;color:#2e7d32}}
.badge-failed{{background:#ffcdd2;color:#c62828}}
.badge-pending{{background:#fff3e0;color:#e65100}}
.badge-skipped{{background:#eceff1;color:#546e6f}}
.badge-dry_run{{background:#fff3e0;color:#e65100}}
.badge-user{{background:#e8eaf6;color:#283593}}
.badge-team{{background:#e0f2f1;color:#00695c}}
.success-rate{{font-weight:600;padding:3px 8px;border-radius:4px}}
.success-rate.high{{background:#d4edda;color:#155724}}
.success-rate.medium{{background:#fff3cd;color:#856404}}
.success-rate.low{{background:#f8d7da;color:#721c24}}
.section-title{{font-size:1.1em;font-weight:600;color:#495057;padding:10px 0;border-bottom:2px solid #e9ecef;margin-bottom:12px;margin-top:20px}}
.filters{{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:15px}}
.filter-select{{padding:8px 12px;border:2px solid #dee2e6;border-radius:6px;font-size:14px;min-width:200px;background:#fff}}
.filter-select:focus{{outline:none;border-color:#667eea}}
.search-box{{padding:8px 12px;border:2px solid #dee2e6;border-radius:6px;font-size:14px;min-width:220px}}
.search-box:focus{{outline:none;border-color:#667eea;box-shadow:0 0 0 3px rgba(102,126,234,.1)}}
.filter-label{{font-weight:600;color:#495057;font-size:.9em}}
.res-group{{margin:15px 0;border:1px solid #e9ecef;border-radius:8px;overflow:hidden}}
.res-group-header{{background:#f8f9fa;padding:12px 16px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;font-weight:600;color:#495057;border-bottom:1px solid #e9ecef}}
.res-group-header:hover{{background:#e8ecff}}
.res-group-header .arrow{{transition:transform .2s;font-size:.8em}}
.res-group-header .arrow.open{{transform:rotate(90deg)}}
.res-group-body{{display:none}}
.res-group-body.open{{display:block}}
.res-item{{border-bottom:1px solid #f0f0f0}}
.res-item-header{{padding:10px 16px 10px 32px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;font-weight:500}}
.res-item-header:hover{{background:#f8f9fa}}
.res-item-roles{{display:none;padding:0 16px 10px 48px}}
.res-item-roles.open{{display:block}}
.role-row{{padding:4px 0;font-size:.88em;color:#495057;display:flex;gap:8px;align-items:center}}
.no-data{{text-align:center;padding:60px 20px;color:#6c757d}}
.error-cell{{max-width:400px;word-wrap:break-word;font-size:.85em;color:#495057}}
.pagination{{display:flex;justify-content:center;align-items:center;gap:10px;padding:15px;margin-top:15px}}
.pagination.hidden{{display:none}}
.pagination button{{padding:8px 16px;border:2px solid #667eea;background:#fff;color:#667eea;border-radius:6px;cursor:pointer;font-weight:600;transition:all .3s}}
.pagination button:hover:not(:disabled){{background:#667eea;color:#fff}}
.pagination button:disabled{{opacity:.3;cursor:not-allowed}}
.pagination .page-info{{padding:0 12px;font-weight:600;color:#495057;font-size:.9em}}
.count-chip{{background:#e9ecef;color:#495057;padding:2px 8px;border-radius:10px;font-size:.8em;font-weight:600}}
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
<button class="tab" data-tab="matrix">Permission Matrix</button>
<button class="tab" data-tab="teams">Team Memberships</button>
<button class="tab" data-tab="sysRoles">System Roles</button>
<button class="tab" data-tab="crossOrg">Cross-Org Sharing</button>
{failures_tab}
</div>
<div class="stats" id="statsContainer"></div>
<div class="content" id="dashboardContent"></div>
<div class="content hidden" id="byOrgContent"></div>
<div class="content hidden" id="byTypeContent"></div>
<div class="content hidden" id="matrixContent"></div>
<div class="content hidden" id="teamsContent"></div>
<div class="content hidden" id="sysRolesContent"></div>
<div class="content hidden" id="crossOrgContent"></div>
{failures_div}
<div class="pagination hidden" id="paginationContainer">
<button id="prevBtn">Previous</button>
<span class="page-info" id="pageInfo">Page 1</span>
<button id="nextBtn">Next</button>
</div>
</div>
<script>
const D={json_block};
const P=D.permissions||[];
const IS_MIG=D.metadata.mode==='migrate'||D.metadata.mode==='dry_run';
const PER_PAGE=100;
let curTab='dashboard',mPage=1,mFiltered=[];
const TYPE_NAMES={{"organizations":"Organizations","teams":"Teams","credentials":"Credentials","projects":"Projects","inventories":"Inventories","job_templates":"Job Templates","workflow_job_templates":"Workflow Templates","notification_templates":"Notifications","instance_groups":"Instance Groups"}};
function tn(t){{return TYPE_NAMES[t]||t.replace(/_/g,' ').replace(/\\b\\w/g,c=>c.toUpperCase())}}
function esc(t){{const d=document.createElement('div');d.textContent=t;return d.innerHTML}}
function badge(s){{return `<span class="badge badge-${{s}}">${{esc(s)}}</span>`}}
function pbadge(t){{return `<span class="badge badge-${{t}}">${{t}}</span>`}}
function init(){{
renderDashboard();
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>switchTab(t.dataset.tab)));
document.getElementById('prevBtn').addEventListener('click',()=>{{if(mPage>1){{mPage--;renderMatrixPage()}}}});
document.getElementById('nextBtn').addEventListener('click',()=>{{const tp=Math.ceil(mFiltered.length/PER_PAGE);if(mPage<tp){{mPage++;renderMatrixPage()}}}});
}}
function switchTab(tab){{
curTab=tab;
document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.dataset.tab===tab));
document.querySelectorAll('.content').forEach(c=>c.classList.add('hidden'));
document.getElementById('paginationContainer').classList.add('hidden');
const el=document.getElementById(tab+'Content');
if(el)el.classList.remove('hidden');
if(tab==='dashboard')renderDashboard();
else if(tab==='byOrg')renderByOrg();
else if(tab==='byType')renderByType();
else if(tab==='matrix')renderMatrix();
else if(tab==='teams')renderTeams();
else if(tab==='sysRoles')renderSysRoles();
else if(tab==='crossOrg')renderCrossOrg();
else if(tab==='failures')renderFailures();
}}
function drillOrg(name){{
switchTab('byOrg');
setTimeout(()=>{{
const sel=document.getElementById('orgDrillSelect');
if(sel){{sel.value=name;sel.dispatchEvent(new Event('change'))}}
}},50);
}}
function renderDashboard(){{
const s=D.stats;
const userCount=P.filter(p=>p.principal_type==='user').length;
const teamCount=P.filter(p=>p.principal_type==='team').length;
let cards=`
<div class="stat-card"><div class="value">${{s.resources_scanned}}</div><div class="label">Resources Scanned</div></div>
<div class="stat-card"><div class="value">${{s.permissions_found}}</div><div class="label">Total Permissions</div></div>
<div class="stat-card"><div class="value">${{teamCount}}</div><div class="label">Team Permissions</div></div>
<div class="stat-card"><div class="value">${{userCount}}</div><div class="label">User Permissions</div></div>
<div class="stat-card"><div class="value">${{s.team_memberships_found}}</div><div class="label">Team Memberships</div></div>
<div class="stat-card"><div class="value">${{s.system_roles_found}}</div><div class="label">System Roles</div></div>`;
if(s.cross_org_shares)cards+=`<div class="stat-card warning"><div class="value">${{s.cross_org_shares}}</div><div class="label">Cross-Org Shares</div></div>`;
if(IS_MIG){{
const attempted=s.permissions_migrated+s.permissions_failed;
const rate=attempted>0?Math.round((s.permissions_migrated/attempted)*100):0;
cards+=`
<div class="stat-card success"><div class="value">${{s.permissions_migrated}}</div><div class="label">Migrated</div></div>
<div class="stat-card danger"><div class="value">${{s.permissions_failed}}</div><div class="label">Failed</div></div>`;
if(s.permissions_skipped)cards+=`<div class="stat-card warning"><div class="value">${{s.permissions_skipped}}</div><div class="label">Pending/Skipped</div></div>`;
cards+=`<div class="stat-card"><div class="value">${{rate}}%</div><div class="label">Success Rate</div></div>`;
}}
document.getElementById('statsContainer').innerHTML=cards;
const orgs=Object.entries(D.org_summaries).sort((a,b)=>b[1].permissions_total-a[1].permissions_total);
let h='<div class="section-title">Organizations</div>';
h+='<table><thead><tr><th>Organization</th><th>Resources</th><th>Permissions</th><th>Team-based</th><th>User-based</th><th>Team Members</th>';
if(IS_MIG)h+='<th>Status</th>';
h+='</tr></thead><tbody>';
orgs.forEach(([name,s])=>{{
const orgPerms=P.filter(p=>p.resource_org===name);
const tPerms=orgPerms.filter(p=>p.principal_type==='team').length;
const uPerms=orgPerms.filter(p=>p.principal_type==='user').length;
let statusHtml='';
if(IS_MIG){{
const r=s.success_rate||0;const cls=r>=80?'high':r>=50?'medium':'low';
statusHtml=`<td><span class="success-rate ${{cls}}">${{r}}%</span></td>`;
}}
h+=`<tr class="clickable" onclick="drillOrg('${{esc(name.replace(/'/g,"\\\\'"))}}')"><td><strong>${{esc(name)}}</strong></td><td>${{s.resources_scanned}}</td><td>${{s.permissions_total}}</td><td>${{tPerms}}</td><td>${{uPerms}}</td><td>${{s.team_memberships_total}}</td>${{statusHtml}}</tr>`;
}});
h+='</tbody></table>';
h+='<p style="color:#6c757d;margin-top:10px;font-size:.85em">Click any organization to view detailed role assignments.</p>';
document.getElementById('dashboardContent').innerHTML=h;
}}
function renderByOrg(){{
document.getElementById('statsContainer').innerHTML='';
const orgs=Object.keys(D.org_summaries).sort();
let h='<div class="filters">';
h+='<span class="filter-label">Organization:</span>';
h+='<select class="filter-select" id="orgDrillSelect"><option value="">-- Select Organization --</option>';
orgs.forEach(o=>{{h+=`<option value="${{esc(o)}}">${{esc(o)}}</option>`}});
h+='</select>';
h+='<span class="filter-label">Type:</span>';
h+='<select class="filter-select" id="orgTypeFilter"><option value="">All Types</option></select>';
h+='<input class="search-box" id="orgDrillSearch" placeholder="Search resources...">';
h+='</div>';
h+='<div id="orgDrillBody"><div class="no-data"><p>Select an organization to view its role assignments.</p></div></div>';
document.getElementById('byOrgContent').innerHTML=h;
document.getElementById('orgDrillSelect').addEventListener('change',renderOrgDrill);
document.getElementById('orgTypeFilter').addEventListener('change',renderOrgDrill);
document.getElementById('orgDrillSearch').addEventListener('input',renderOrgDrill);
}}
function renderOrgDrill(){{
const org=document.getElementById('orgDrillSelect').value;
const typeFilter=document.getElementById('orgTypeFilter').value;
const search=document.getElementById('orgDrillSearch').value.toLowerCase();
const body=document.getElementById('orgDrillBody');
if(!org){{body.innerHTML='<div class="no-data"><p>Select an organization to view its role assignments.</p></div>';return}}
let perms=P.filter(p=>p.resource_org===org);
const types=[...new Set(perms.map(p=>p.resource_type))].sort();
const typeSel=document.getElementById('orgTypeFilter');
const curType=typeSel.value;
let opts='<option value="">All Types</option>';
types.forEach(t=>{{opts+=`<option value="${{t}}" ${{t===curType?'selected':''}}>${{tn(t)}}</option>`}});
typeSel.innerHTML=opts;
if(typeFilter)perms=perms.filter(p=>p.resource_type===typeFilter);
if(search)perms=perms.filter(p=>p.resource_name.toLowerCase().includes(search)||p.principal_name.toLowerCase().includes(search)||p.role_name.toLowerCase().includes(search));
if(!perms.length){{body.innerHTML='<div class="no-data"><p>No permissions found for this selection.</p></div>';return}}
const s=D.org_summaries[org]||{{}};
const teamPerms=perms.filter(p=>p.principal_type==='team').length;
const userPerms=perms.filter(p=>p.principal_type==='user').length;
let h=`<div style="display:flex;gap:15px;flex-wrap:wrap;margin-bottom:15px">`;
h+=`<div class="stat-card" style="padding:12px 18px;flex:1;min-width:120px"><div class="value" style="font-size:1.5em">${{perms.length}}</div><div class="label">Permissions</div></div>`;
h+=`<div class="stat-card" style="padding:12px 18px;flex:1;min-width:120px;background:linear-gradient(135deg,#e0f2f1,#b2dfdb)"><div class="value" style="font-size:1.5em;color:#00695c">${{teamPerms}}</div><div class="label" style="color:#00695c">Team-based</div></div>`;
h+=`<div class="stat-card" style="padding:12px 18px;flex:1;min-width:120px;background:linear-gradient(135deg,#e8eaf6,#c5cae9)"><div class="value" style="font-size:1.5em;color:#283593">${{userPerms}}</div><div class="label" style="color:#283593">User-based</div></div>`;
h+=`</div>`;
const byType={{}};
perms.forEach(p=>{{
if(!byType[p.resource_type])byType[p.resource_type]={{}};
const key=p.resource_id+'_'+p.resource_name;
if(!byType[p.resource_type][key])byType[p.resource_type][key]={{name:p.resource_name,id:p.resource_id,roles:[]}};
byType[p.resource_type][key].roles.push(p);
}});
Object.entries(byType).sort((a,b)=>a[0].localeCompare(b[0])).forEach(([rtype,resources])=>{{
const resArr=Object.values(resources).sort((a,b)=>a.name.localeCompare(b.name));
const totalPerms=resArr.reduce((s,r)=>s+r.roles.length,0);
const gid='g_'+rtype.replace(/\\W/g,'');
h+=`<div class="res-group">`;
h+=`<div class="res-group-header" onclick="toggleGroup('${{gid}}')"><span><span class="arrow open" id="arr_${{gid}}">&#9654;</span> ${{esc(tn(rtype))}} <span class="count-chip">${{resArr.length}} resources, ${{totalPerms}} permissions</span></span></div>`;
h+=`<div class="res-group-body open" id="body_${{gid}}">`;
resArr.forEach((res,ri)=>{{
const rid=gid+'_r'+ri;
h+=`<div class="res-item">`;
h+=`<div class="res-item-header" onclick="toggleItem('${{rid}}')"><span><span class="arrow" id="arr_${{rid}}">&#9654;</span> ${{esc(res.name)}}</span><span class="count-chip">${{res.roles.length}} roles</span></div>`;
h+=`<div class="res-item-roles" id="body_${{rid}}">`;
res.roles.sort((a,b)=>a.role_name.localeCompare(b.role_name)).forEach(r=>{{
h+=`<div class="role-row"><strong>${{esc(r.role_name)}}</strong> &rarr; ${{pbadge(r.principal_type)}} <strong>${{esc(r.principal_name)}}</strong>`;
if(IS_MIG)h+=` ${{badge(r.status)}}`;
h+=`</div>`;
}});
h+=`</div></div>`;
}});
h+=`</div></div>`;
}});
body.innerHTML=h;
}}
function toggleGroup(id){{
const b=document.getElementById('body_'+id);
const a=document.getElementById('arr_'+id);
if(b){{b.classList.toggle('open');if(a)a.classList.toggle('open')}}
}}
function toggleItem(id){{
const b=document.getElementById('body_'+id);
const a=document.getElementById('arr_'+id);
if(b){{b.classList.toggle('open');if(a)a.classList.toggle('open')}}
}}
function renderByType(){{
document.getElementById('statsContainer').innerHTML='';
const types=Object.entries(D.type_summaries).sort((a,b)=>b[1].permission_count-a[1].permission_count);
let h='';
types.forEach(([key,t])=>{{
const perms=P.filter(p=>p.resource_type===key);
const resources={{}};
perms.forEach(p=>{{
const k=p.resource_id+'_'+p.resource_name;
if(!resources[k])resources[k]={{name:p.resource_name,org:p.resource_org,roles:[]}};
resources[k].roles.push(p);
}});
const resArr=Object.values(resources).sort((a,b)=>a.name.localeCompare(b.name));
const gid='t_'+key.replace(/\\W/g,'');
h+=`<div class="res-group">`;
h+=`<div class="res-group-header" onclick="toggleGroup('${{gid}}')"><span><span class="arrow open" id="arr_${{gid}}">&#9654;</span> ${{esc(t.display||key)}} <span class="count-chip">${{t.resource_count}} resources, ${{t.permission_count}} permissions</span></span></div>`;
h+=`<div class="res-group-body open" id="body_${{gid}}">`;
h+=`<table><thead><tr><th>Resource</th><th>Organization</th><th>Role</th><th>Assigned To</th><th>Type</th>`;
if(IS_MIG)h+=`<th>Status</th>`;
h+=`</tr></thead><tbody>`;
resArr.forEach(res=>{{
res.roles.sort((a,b)=>a.role_name.localeCompare(b.role_name)).forEach((r,i)=>{{
h+=`<tr><td>${{i===0?'<strong>'+esc(res.name)+'</strong>':''}}</td><td>${{i===0?esc(res.org):''}}</td><td>${{esc(r.role_name)}}</td><td>${{esc(r.principal_name)}}</td><td>${{pbadge(r.principal_type)}}</td>`;
if(IS_MIG)h+=`<td>${{badge(r.status)}}</td>`;
h+=`</tr>`;
}});
}});
h+=`</tbody></table></div></div>`;
}});
document.getElementById('byTypeContent').innerHTML=h;
}}
function renderMatrix(){{
document.getElementById('statsContainer').innerHTML='';
const orgs=[...new Set(P.map(p=>p.resource_org))].sort();
const types=[...new Set(P.map(p=>p.resource_type))].sort();
const roles=[...new Set(P.map(p=>p.role_name))].sort();
let h='<div class="filters">';
h+='<span class="filter-label">Org:</span><select class="filter-select" id="mOrg" style="min-width:160px"><option value="">All</option>';
orgs.forEach(o=>{{h+=`<option value="${{esc(o)}}">${{esc(o)}}</option>`}});
h+='</select>';
h+='<span class="filter-label">Type:</span><select class="filter-select" id="mType" style="min-width:160px"><option value="">All</option>';
types.forEach(t=>{{h+=`<option value="${{t}}">${{tn(t)}}</option>`}});
h+='</select>';
h+='<span class="filter-label">Role:</span><select class="filter-select" id="mRole" style="min-width:120px"><option value="">All</option>';
roles.forEach(r=>{{h+=`<option value="${{esc(r)}}">${{esc(r)}}</option>`}});
h+='</select>';
h+='<span class="filter-label">Principal:</span><select class="filter-select" id="mPrincipal" style="min-width:100px"><option value="">All</option><option value="user">Users</option><option value="team">Teams</option></select>';
h+='<input class="search-box" id="mSearch" placeholder="Search name...">';
h+='</div>';
h+='<div id="matrixInfo" style="color:#6c757d;font-size:.85em;margin-bottom:8px"></div>';
h+='<div id="matrixTable"></div>';
document.getElementById('matrixContent').innerHTML=h;
['mOrg','mType','mRole','mPrincipal'].forEach(id=>document.getElementById(id).addEventListener('change',applyMatrixFilter));
document.getElementById('mSearch').addEventListener('input',applyMatrixFilter);
applyMatrixFilter();
}}
function applyMatrixFilter(){{
const org=document.getElementById('mOrg').value;
const type=document.getElementById('mType').value;
const role=document.getElementById('mRole').value;
const principal=document.getElementById('mPrincipal').value;
const search=document.getElementById('mSearch').value.toLowerCase();
mFiltered=P.filter(p=>{{
if(org&&p.resource_org!==org)return false;
if(type&&p.resource_type!==type)return false;
if(role&&p.role_name!==role)return false;
if(principal&&p.principal_type!==principal)return false;
if(search&&!p.resource_name.toLowerCase().includes(search)&&!p.principal_name.toLowerCase().includes(search))return false;
return true;
}});
mPage=1;
document.getElementById('matrixInfo').textContent=`Showing ${{mFiltered.length}} of ${{P.length}} permissions`;
renderMatrixPage();
}}
function renderMatrixPage(){{
const start=(mPage-1)*PER_PAGE;const end=start+PER_PAGE;
const page=mFiltered.slice(start,end);
let h='<table><thead><tr><th>Organization</th><th>Resource Type</th><th>Resource Name</th><th>Role</th><th>Principal</th><th>Type</th>';
if(IS_MIG)h+='<th>Status</th>';
h+='</tr></thead><tbody>';
if(!page.length)h+='<tr><td colspan="7" style="text-align:center;padding:30px;color:#6c757d">No permissions match the current filters.</td></tr>';
page.forEach(p=>{{
h+=`<tr><td>${{esc(p.resource_org)}}</td><td>${{esc(tn(p.resource_type))}}</td><td><strong>${{esc(p.resource_name)}}</strong></td><td>${{esc(p.role_name)}}</td><td>${{esc(p.principal_name)}}</td><td>${{pbadge(p.principal_type)}}</td>`;
if(IS_MIG)h+=`<td>${{badge(p.status)}}</td>`;
h+='</tr>';
}});
h+='</tbody></table>';
document.getElementById('matrixTable').innerHTML=h;
const totalPages=Math.ceil(mFiltered.length/PER_PAGE);
const pg=document.getElementById('paginationContainer');
if(totalPages>1&&curTab==='matrix'){{
pg.classList.remove('hidden');
document.getElementById('pageInfo').textContent=`Page ${{mPage}} of ${{totalPages}} (${{start+1}}-${{Math.min(end,mFiltered.length)}} of ${{mFiltered.length}})`;
document.getElementById('prevBtn').disabled=mPage===1;
document.getElementById('nextBtn').disabled=mPage===totalPages;
}}else{{pg.classList.add('hidden')}}
}}
function renderTeams(){{
document.getElementById('statsContainer').innerHTML='';
const teams=Object.entries(D.team_summary).sort((a,b)=>b[1].members.length-a[1].members.length);
if(!teams.length){{document.getElementById('teamsContent').innerHTML='<div class="no-data"><p>No team memberships found.</p></div>';return}}
const byOrg={{}};
teams.forEach(([key,t])=>{{
if(!byOrg[t.org])byOrg[t.org]=[];
byOrg[t.org].push(t);
}});
let h='<input class="search-box" id="teamSearch" placeholder="Search teams or users..." oninput="filterTeamRows()">';
Object.entries(byOrg).sort((a,b)=>a[0].localeCompare(b[0])).forEach(([org,orgTeams])=>{{
h+=`<div class="section-title">${{esc(org)}}</div>`;
h+='<table><thead><tr><th>Team</th><th>Members</th><th>Member List</th>';
if(IS_MIG)h+='<th>Migrated</th><th>Failed</th>';
h+='</tr></thead><tbody class="teamFilterable">';
orgTeams.sort((a,b)=>a.team_name.localeCompare(b.team_name)).forEach(t=>{{
const memberHtml=t.members.map(m=>{{
let s=esc(m.username);
if(IS_MIG)s+=` ${{badge(m.status)}}`;
return s;
}}).join(', ');
let extra='';
if(IS_MIG)extra=`<td>${{t.migrated}}</td><td>${{t.failed}}</td>`;
h+=`<tr data-search="${{esc((t.team_name+' '+t.members.map(m=>m.username).join(' ')).toLowerCase())}}"><td><strong>${{esc(t.team_name)}}</strong></td><td>${{t.members.length}}</td><td style="font-size:.85em">${{memberHtml}}</td>${{extra}}</tr>`;
}});
h+='</tbody></table>';
}});
document.getElementById('teamsContent').innerHTML=h;
}}
function filterTeamRows(){{
const q=document.getElementById('teamSearch').value.toLowerCase();
document.querySelectorAll('.teamFilterable tr').forEach(r=>{{
r.style.display=(r.dataset.search||'').includes(q)?'':'none';
}});
}}
function renderSysRoles(){{
document.getElementById('statsContainer').innerHTML='';
const roles=D.system_roles;
if(!roles.length){{document.getElementById('sysRolesContent').innerHTML='<div class="no-data"><p>No system-level roles detected.</p></div>';return}}
let h='<table><thead><tr><th>Username</th><th>User ID</th><th>Role</th></tr></thead><tbody>';
roles.forEach(r=>{{
const label=r.flag==='is_superuser'?'System Administrator':'System Auditor';
h+=`<tr><td><strong>${{esc(r.username)}}</strong></td><td>${{r.user_id}}</td><td>${{badge('audit')}} ${{esc(label)}}</td></tr>`;
}});
h+='</tbody></table>';
document.getElementById('sysRolesContent').innerHTML=h;
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
function renderFailures(){{
const perms=P.filter(p=>p.status==='failed');
const mems=D.membership_failures||[];
const total=perms.length+mems.length;
document.getElementById('statsContainer').innerHTML=`<div class="stat-card danger"><div class="value">${{total}}</div><div class="label">Total Failures</div></div>`;
if(!total){{document.getElementById('failuresContent').innerHTML='<div class="no-data"><p>No failures recorded.</p></div>';return}}
let h='';
if(mems.length){{
h+='<div class="section-title">Team Membership Failures</div>';
h+='<table><thead><tr><th>Team</th><th>Organization</th><th>Username</th><th>Error</th></tr></thead><tbody>';
mems.forEach(m=>{{h+=`<tr><td>${{esc(m.team_name)}}</td><td>${{esc(m.team_org)}}</td><td>${{esc(m.username)}}</td><td class="error-cell">${{esc(m.error)}}</td></tr>`}});
h+='</tbody></table>';
}}
if(perms.length){{
h+='<div class="section-title">Permission Failures</div>';
h+='<table><thead><tr><th>Organization</th><th>Resource</th><th>Type</th><th>Role</th><th>Principal</th><th>Error</th></tr></thead><tbody>';
perms.forEach(p=>{{
h+=`<tr><td>${{esc(p.resource_org)}}</td><td><strong>${{esc(p.resource_name)}}</strong></td><td>${{esc(tn(p.resource_type))}}</td><td>${{esc(p.role_name)}}</td><td>${{esc(p.principal_name)}} (${{esc(p.principal_type)}})</td><td class="error-cell">${{esc(p.error)}}</td></tr>`;
}});
h+='</tbody></table>';
}}
document.getElementById('failuresContent').innerHTML=h;
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
