"""Validation report generator v3 — visual redesign.

Single JSON blob + lazy client-side rendering. Visual elements:
  - Migration funnel (CSS bars)
  - Per-type progress bars
  - Org dot-grid heatmap
  - Paginated tables with drill-in
  - Display convention: **Name** · Org [src:id → tgt:id]
"""

from __future__ import annotations

import json
import os
from html import escape
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from aap_migration.validate.common import count_failed_resource_types
from aap_migration.validate.models import ValidationResult


def _hostname_only(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    return parsed.hostname or url


def _safe_json_embed(data: Any) -> str:
    raw = json.dumps(data, indent=2, default=str)
    return raw.replace("</", "<\\/")


def _write_secure(path: str, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)


def _fmt(n: int) -> str:
    return f"{n:,}"


def export_validation_json(result: ValidationResult, output_path: str) -> None:
    _write_secure(output_path, json.dumps(result.to_dict(), indent=2, default=str))


def generate_validation_html(result: ValidationResult, field_data: dict | None = None) -> str:
    d = result.to_dict()
    meta = result.metadata
    es = result.executive_summary
    source_host = escape(_hostname_only(meta.source_url))
    target_host = escape(_hostname_only(meta.target_url))
    is_mock = meta.mode in ("dry-run-mock", "dry-run-db")
    is_live_target = meta.mode in ("dry-run-live-target", "validate-live")
    if is_live_target:
        fd_source_label = "Live API (AAP 2.6)"
    elif meta.mode in ("dry-run-db", "validate-db"):
        fd_source_label = "Database (migration_state.db — import status only)"
    else:
        fd_source_label = "Transformed payloads (xformed/)"

    total_source = sum(t.t1_counts.source for t in result.per_type)
    total_missing = sum(t.t2_existence.missing_on_target for t in result.per_type)
    total_extra_listed = sum(
        len(t.t2_existence.extra_details) + (t.t2_existence.extra_truncated_count or 0)
        for t in result.per_type
    )
    total_field_mm = sum(t.t3_field_parity.mismatching for t in result.per_type)
    total_sync_failed = sum(1 for s in result.sync_entries if s.failed)
    types_failed = count_failed_resource_types(result.per_type)
    n_types = len(result.per_type)
    n_orgs = len(result.per_org)
    orgs_red = sum(1 for o in result.per_org.values() if o.health == "red")
    orgs_amber = sum(1 for o in result.per_org.values() if o.health == "amber")
    orgs_green = n_orgs - orgs_red - orgs_amber
    orgs_explained_failures = sum(
        o.explained_failures for o in result.per_org.values()
    )
    scoped_orgs = list(meta.organizations or [])
    scope_banner = ""
    scope_callout = ""
    if scoped_orgs:
        scope_label = ", ".join(scoped_orgs)
        if len(scoped_orgs) == 1:
            scope_banner = (
                f'<span title="Limited via --orgs">'
                f"Organizations checked: {escape(scope_label)}</span>"
            )
            scope_callout = (
                f'<div class="callout callout-info">'
                f"<strong>Organizations checked:</strong> {escape(scope_label)}. "
                f"Pure globals and auditor verification are skipped for org-scoped runs."
                f"</div>"
            )
        else:
            scope_banner = (
                f'<span title="Limited via --orgs (multi-org)">'
                f"Organizations checked ({len(scoped_orgs)}): "
                f"{escape(scope_label)}</span>"
            )
            scope_callout = (
                f'<div class="callout callout-info">'
                f"<strong>Organizations checked ({len(scoped_orgs)}, multi-org run):</strong> "
                f"{escape(scope_label)}. "
                f"This combined report covers these organizations together; "
                f"per-organization reports are also written beside this folder "
                f"as <code>&lt;org-name&gt;/report.html</code>. "
                f"Pure globals and auditor verification are skipped."
                f"</div>"
            )
    org_scope_method = (
        f"Organizations checked: {', '.join(scoped_orgs)}"
        + (" (multi-org run)" if len(scoped_orgs) > 1 else "")
        if scoped_orgs
        else "All organizations"
    )
    json_block = _safe_json_embed(d)
    inv_raw = json.dumps(result.inventory_to_dict(), separators=(",", ":"), default=str)
    inv_block = inv_raw.replace("</", "<\\/")
    if field_data:
        fd_raw = json.dumps(field_data, separators=(",", ":"), default=str)
        fd_block = fd_raw.replace("</", "<\\/")
    else:
        fd_block = "{}"
    verdict_class = "" if es.verdict == "PASS" else " review"
    verdict_icon = "&#10003;" if es.verdict == "PASS" else "&#9888;"

    mock_banner = ""
    mock_css = ""
    if is_mock:
        mock_banner = (
            '<div class="mock-banner">'
            "&#9888; DRY-RUN-MOCK &mdash; Synthetic data, not a live validation run &#9888;"
            "</div>"
        )
        mock_css = (
            ".mock-banner{background:#e65100;color:#fff;text-align:center;"
            "padding:10px;font-weight:700;font-size:1rem;letter-spacing:1px;"
            "position:sticky;top:0;z-index:200;}"
        )

    banner_top = "42px" if is_mock else "0"
    nav_top = "115px" if is_mock else "73px"

    exc = meta.exclusion_sets
    type_overrides_str = " | ".join(
        f"{escape(k)}: {', '.join(escape(v) for v in vals)}"
        for k, vals in exc.type_specific_overrides.items()
    ) or "None"
    tiers_str = ", ".join(meta.tiers_run) if meta.tiers_run else "None"

    # Host T4 data for client-side inventory filter (mismatch default + searchable name)
    hs = result.t4_host_sampling
    inv_parity = hs.per_inventory_count_parity
    host_matched = hs.matched_hosts
    host_missing = hs.missing_hosts
    host_issues = (
        host_missing
        + inv_parity.mismatching
        + hs.field_mismatches_in_sample
    )
    host_t4_payload = {
        "total_hosts_source": hs.total_hosts_source,
        "total_hosts_target": hs.total_hosts_target,
        "matched_hosts": host_matched,
        "missing_hosts": host_missing,
        "inventories_checked": hs.inventories_checked,
        "sample_size": hs.sample_size,
        "field_mismatches_in_sample": hs.field_mismatches_in_sample,
        "confidence": hs.confidence,
        "matching": inv_parity.matching,
        "mismatching": inv_parity.mismatching,
        "inventories": [d.to_dict() for d in inv_parity.details],
        "ran": bool(
            hs.total_hosts_source
            or hs.total_hosts_target
            or hs.inventories_checked
            or hs.sample_size
        ),
    }
    host_t4_raw = json.dumps(host_t4_payload, separators=(",", ":"), default=str)
    host_t4_block = host_t4_raw.replace("</", "<\\/")

    # Server-render auditor (small fixed data)
    ac = result.auditor_cross_check
    aud_rows = []
    for ad in ac.details:
        gw = "&#10003; Assigned" if ad.gateway_has_platform_auditor else "&#10007; Not assigned"
        vtag = '<span class="tag tag-match">MATCH</span>' if ad.match else '<span class="tag tag-miss">MISMATCH</span>'
        src_str = str(ad.source_id) if ad.source_id is not None else "—"
        tgt_str = str(ad.target_id) if ad.target_id is not None else "—"
        aud_rows.append(
            f"<tr><td><strong>{escape(ad.username)}</strong>"
            f' <span class="ids">[src:{src_str} → tgt:{tgt_str}]</span></td>'
            f"<td>{gw}</td><td>{vtag}</td></tr>"
        )
    if aud_rows:
        aud_html = "\n".join(aud_rows)
    elif scoped_orgs and meta.mode == "validate-live":
        aud_html = (
            '<tr><td colspan="4" class="empty-msg">'
            "Auditor check skipped for org-scoped validation."
            "</td></tr>"
        )
    else:
        aud_html = (
            '<tr><td colspan="4" class="empty-msg">'
            "No auditor data available."
            "</td></tr>"
        )

    is_live_validate = meta.mode == "validate-live"
    host_tab_ran = bool(host_t4_payload["ran"])
    scope_cov = (
        "selected organization scope (pure globals omitted)"
        if scoped_orgs
        else "all exported types and organizations in this run"
    )
    t1_coverage = f"100% &mdash; {scope_cov}"
    t2_coverage = (
        "100% &mdash; in-scope identity checks (missing on target + extra on target)"
    )
    t3_coverage = (
        "100% of matched non-host objects (live mode)"
        if is_live_validate
        else "Not run in database-only mode (requires --live)"
    )
    t4_coverage = (
        "100% host existence + stratified field sample of matched hosts"
        if host_tab_ran
        else "Not run (hosts skipped or unavailable for this run)"
    )
    auditor_rule_effect = (
        "Verified via Gateway role assignments"
        if (is_live_validate and not scoped_orgs)
        else "Skipped for org-scoped or non-live runs"
    )
    confidence_note = (
        f"Host sample confidence is a design target ({escape(hs.confidence)} at "
        f"{escape(hs.margin_of_error)} MoE), based on Cochran sizing over matched hosts "
        "with stratified-by-inventory sampling and fixed seed for reproducibility; "
        "it is not a posterior confidence recalculated from observed mismatches."
    )
    if meta.mode == "validate-live" and scoped_orgs:
        auditor_tab_badge = (
            ' <span class="ct skip" title="Skipped for org-scoped validation">'
            "skipped</span>"
        )
    elif meta.mode == "validate-live":
        auditor_tab_badge = (
            f' <span class="ct{" ok" if ac.mismatches == 0 else ""}">'
            f"{_fmt(ac.mismatches)}</span>"
        )
    else:
        auditor_tab_badge = ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AAP Migration Validation &mdash; {escape(meta.run_id)}</title>
<style>
:root{{
  --bg:#f8f9fa;--card:#fff;--fg:#1a1a2e;--fg2:#57606a;
  --accent:#0f3460;--accent2:#16213e;--border:#d0d7de;
  --code-bg:#f6f8fa;--stripe:#f8f9fb;
  /* Ansible / PatternFly status colours */
  --pass:#3e8635;--pass-bg:#f3faf2;
  --warn:#f0ab00;--warn-bg:#fdf7e7;
  --fail:#c9190b;--fail-bg:#faeae8;
  --skip:#6a6e73;--skip-bg:#f0f0f0;
  --info:#0066cc;--info-bg:#e7f1fa;
  --shadow:0 1px 3px rgba(0,0,0,.1);
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:var(--fg);background:var(--bg);font-size:14px;line-height:1.5}}
.hidden{{display:none!important}}
{mock_css}

/* ── Banner + Nav ── */
.shell{{max-width:1300px;margin:0 auto;padding:0 2rem;width:100%;box-sizing:border-box}}
.banner{{background:var(--card);border-bottom:3px solid var(--pass);padding:1rem 0;box-shadow:var(--shadow);position:sticky;top:{banner_top};z-index:100}}
.banner.review{{border-bottom-color:var(--fail)}}
.banner .shell{{display:flex;align-items:center;gap:1rem}}
.verdict-badge{{background:var(--pass);color:#fff;padding:.35rem 1.1rem;border-radius:6px;font-size:1.05rem;font-weight:700;white-space:nowrap}}
.banner.review .verdict-badge{{background:var(--fail)}}
.banner-info{{flex:1;min-width:0}}.banner-title{{font-size:1.05rem;font-weight:600}}
.banner-meta{{font-size:.8rem;color:var(--fg2);margin-top:2px;display:flex;flex-wrap:wrap;gap:2px 1.2rem}}
.banner-meta span{{margin-right:0}}

nav{{background:var(--card);border-bottom:1px solid var(--border);padding:0;position:sticky;top:{nav_top};z-index:99}}
nav .shell{{display:flex;gap:0;overflow-x:auto}}
.tab-btn{{background:none;border:none;padding:.6rem 1rem;font-size:.82rem;font-weight:500;color:var(--fg2);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;transition:all .15s}}
.tab-btn:hover{{color:var(--accent);background:rgba(15,52,96,.03)}}
.tab-btn.active{{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}}
.tab-btn .ct{{display:inline-block;background:var(--fail);color:#fff;font-size:.68rem;font-weight:700;padding:1px 5px;border-radius:8px;margin-left:3px;vertical-align:middle}}
.tab-btn .ct.ok{{background:var(--pass)}}
.tab-btn .ct.warn{{background:var(--warn);color:#1a1a2e}}
.tab-btn .ct.skip{{background:var(--skip)}}

.container{{max-width:1300px;margin:0 auto;padding:1.2rem 2rem 3rem}}
.content{{padding:.5rem 0}}

/* ── Cards ── */
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.7rem;margin:.8rem 0}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:.8rem;text-align:center;box-shadow:var(--shadow)}}
.card .v{{font-size:1.6rem;font-weight:700;color:var(--accent)}}
.card .v.ok{{color:var(--pass)}}.card .v.bad{{color:var(--fail)}}
.card .v.warn{{color:var(--warn)}}.card .v.skip{{color:var(--skip)}}
.card .l{{font-size:.75rem;color:var(--fg2);margin-top:1px}}

/* ── Funnel ── */
.funnel{{margin:1.2rem 0}}.funnel-step{{display:flex;align-items:center;margin:6px 0;gap:10px}}
.funnel-label{{width:120px;font-size:.82rem;color:var(--fg2);font-weight:500;text-align:right;flex-shrink:0}}
.funnel-track{{flex:1;min-width:0;height:32px;background:#e9ecef;border-radius:6px;overflow:hidden}}
.funnel-bar{{height:100%;border-radius:6px;box-sizing:border-box;min-width:0;max-width:100%;transition:width .4s}}
.funnel-count{{font-size:.8rem;color:var(--fg2);white-space:nowrap;width:5.5rem;flex-shrink:0;font-variant-numeric:tabular-nums}}

/* ── Progress bars (per-type) ── */
.pbar-row{{display:flex;align-items:center;padding:5px 0;border-bottom:1px solid #eee}}
.pbar-name{{width:180px;font-size:.82rem;font-weight:500;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.pbar-track{{flex:1;height:18px;background:#e9ecef;border-radius:4px;overflow:hidden;display:flex;margin:0 10px}}
.pbar-seg{{height:100%;transition:width .3s}}
.pbar-nums{{font-size:.78rem;color:var(--fg2);white-space:nowrap;width:180px;text-align:right;flex-shrink:0}}
.pbar-row:hover{{background:rgba(15,52,96,.04);border-radius:6px}}

/* ── T1 type cards: count parity + field object parity ── */
.type-card{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:.8rem;margin:.5rem 0;box-shadow:var(--shadow);cursor:pointer}}
.type-card:hover{{border-color:var(--accent);background:rgba(15,52,96,.02)}}
.type-card-head{{display:flex;justify-content:space-between;align-items:center;gap:.5rem}}
.type-card-head strong{{font-size:.9rem}}
.type-card-meta{{font-size:.78rem;color:var(--fg2);flex-shrink:0}}
.type-bar-row{{display:flex;align-items:center;gap:.6rem;margin-top:.45rem}}
.type-bar-label{{width:4.2rem;font-size:.72rem;color:var(--fg2);flex-shrink:0}}
.type-bar-track{{flex:1;min-width:0;height:14px;background:#e9ecef;border-radius:4px;overflow:hidden;display:flex}}
.type-bar-seg{{height:100%;min-width:0}}
.type-bar-seg.ok{{background:var(--pass)}}
.type-bar-seg.bad{{background:var(--fail)}}
.type-bar-seg.warn{{background:var(--warn)}}
.type-bar-nums{{font-size:.72rem;color:var(--fg2);white-space:nowrap;width:9.5rem;text-align:right;flex-shrink:0;font-variant-numeric:tabular-nums}}
.type-card-stats{{display:flex;flex-wrap:wrap;gap:1rem;margin-top:.45rem;font-size:.78rem;color:var(--fg2)}}
.type-legend{{display:flex;flex-wrap:wrap;gap:1rem;font-size:.75rem;color:var(--fg2);margin:.4rem 0 .8rem}}
.type-legend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}}
.type-legend .ok{{background:var(--pass)}}.type-legend .bad{{background:var(--fail)}}.type-legend .warn{{background:var(--warn)}}

/* ── Dot grid ── */
.dot-grid{{display:flex;flex-wrap:wrap;gap:3px;padding:8px;background:var(--card);border:1px solid var(--border);border-radius:8px;margin:.8rem 0;box-shadow:var(--shadow)}}
.dot{{width:10px;height:10px;border-radius:2px;cursor:pointer;transition:transform .1s;position:relative}}
.dot:hover{{transform:scale(2);z-index:10}}
.dot.g{{background:var(--pass)}}.dot.a{{background:var(--warn)}}.dot.r{{background:var(--fail)}}
.dot-legend{{display:flex;gap:1rem;font-size:.78rem;color:var(--fg2);margin:4px 0}}
.dot-legend span::before{{content:'';display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:middle}}
.dot-legend .lg::before{{background:var(--pass)}}.dot-legend .la::before{{background:var(--warn)}}.dot-legend .lr::before{{background:var(--fail)}}

/* ── Stacked bar (org drill-in) ── */
.sbar{{height:22px;display:flex;border-radius:4px;overflow:hidden;margin:2px 0}}
.sbar-s{{background:var(--pass);height:100%}}.sbar-m{{background:var(--fail);height:100%}}.sbar-c{{background:var(--warn);height:100%}}.sbar-u{{background:var(--skip);height:100%}}

/* ── Tables ── */
table{{width:100%;border-collapse:collapse;margin:.5rem 0;font-size:.83rem;background:var(--card);border-radius:8px;overflow:hidden;box-shadow:var(--shadow)}}
thead th{{background:var(--accent2);color:#fff;text-align:left;padding:.45rem .6rem;font-weight:600;font-size:.78rem;user-select:none;white-space:nowrap}}
thead th.num{{text-align:right}}
td{{padding:.35rem .6rem;border-bottom:1px solid #eee;vertical-align:top}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
tr:nth-child(even){{background:var(--stripe)}}
tr:hover{{background:#eef1f6}}
tr.clickable{{cursor:pointer}}tr.clickable:hover{{background:#dde4f0}}
.empty-msg{{text-align:center;color:var(--fg2);padding:1.5rem!important}}

/* ── Common ── */
h2{{font-size:1.1rem;color:var(--accent);margin:1.2rem 0 .6rem}}
h3{{font-size:.92rem;color:var(--accent2);margin:1rem 0 .4rem}}
.ids{{font-size:.73rem;color:var(--fg2);font-family:'SF Mono',Consolas,monospace;margin-left:3px}}
code{{font-family:'SF Mono',Consolas,monospace;font-size:.83em;background:var(--code-bg);padding:1px 4px;border-radius:3px;border:1px solid #e1e4e8}}
.callout{{padding:.5rem .8rem;border-radius:6px;margin:.6rem 0;font-size:.85rem}}
.callout-info{{background:var(--info-bg);border-left:3px solid var(--info)}}
.callout-pass{{background:var(--pass-bg);border-left:3px solid var(--pass)}}
.callout-warn{{background:var(--warn-bg);border-left:3px solid var(--warn)}}
.callout-fail{{background:var(--fail-bg);border-left:3px solid var(--fail)}}
.tag{{display:inline-block;font-size:.7rem;font-weight:600;padding:1px 6px;border-radius:10px;vertical-align:middle}}
.tag-match{{background:var(--pass-bg);color:var(--pass)}}.tag-miss{{background:var(--fail-bg);color:var(--fail)}}.tag-changed{{background:var(--warn-bg);color:var(--warn)}}
.health{{display:inline-block;font-size:.7rem;font-weight:700;padding:2px 8px;border-radius:10px;text-transform:uppercase}}
.h-r{{background:var(--fail-bg);color:var(--fail)}}.h-a{{background:var(--warn-bg);color:var(--warn)}}.h-g{{background:var(--pass-bg);color:var(--pass)}}
.meta-grid{{display:grid;grid-template-columns:140px 1fr;gap:.15rem .8rem;font-size:.85rem;margin:.4rem 0}}
.meta-grid .k{{color:var(--fg2);font-weight:500}}.meta-grid .v{{color:var(--fg)}}

/* ── Finding cards ── */
.finding{{background:var(--card);border:1px solid var(--border);border-radius:8px;margin:.5rem 0;overflow:hidden;box-shadow:var(--shadow)}}
.finding-hd{{background:var(--code-bg);padding:.4rem .8rem;display:flex;justify-content:space-between;align-items:center;font-size:.82rem;border-bottom:1px solid var(--border)}}
.finding-bd{{padding:.6rem .8rem}}.finding-bd table{{box-shadow:none;margin:0}}
.finding-bd td{{border-bottom:1px solid #f0f0f0;padding:.25rem .4rem}}.finding-bd td:first-child{{font-weight:600;width:110px;color:var(--fg2);font-size:.8rem}}
.src-val{{color:var(--fg);white-space:pre-wrap;word-break:break-word}}.tgt-val{{color:var(--fg);white-space:pre-wrap;word-break:break-word}}

/* ── Collapsible groups ── */
.grp-hd{{background:var(--code-bg);padding:.5rem .8rem;cursor:pointer;display:flex;align-items:center;gap:.5rem;border:1px solid var(--border);border-radius:6px;margin:.4rem 0;font-size:.85rem;font-weight:500;user-select:none}}
.grp-hd:hover{{background:#e8ecf1}}.grp-hd .arrow{{transition:transform .15s;font-size:.7rem}}.grp-hd.open .arrow{{transform:rotate(90deg)}}
.grp-bd{{padding:.2rem 0 .2rem 1.2rem;display:none}}.grp-bd.open{{display:block}}

/* ── Pagination ── */
.pager{{display:flex;align-items:center;gap:.8rem;margin:.8rem 0;font-size:.82rem}}
.pager button{{background:var(--accent);color:#fff;border:none;padding:.3rem .8rem;border-radius:4px;cursor:pointer;font-size:.8rem}}.pager button:disabled{{opacity:.4;cursor:default}}
.pager .pg-info{{color:var(--fg2)}}

/* ── Search / filter bar ── */
.filter-bar{{display:flex;gap:.6rem;margin:.6rem 0;align-items:center}}
.filter-bar input{{flex:1;padding:8px 12px;border:2px solid var(--border);border-radius:6px;font-size:13px}}.filter-bar input:focus{{outline:none;border-color:var(--accent)}}
.filter-bar select{{padding:7px 10px;border:2px solid var(--border);border-radius:6px;font-size:13px;background:var(--card)}}
.filter-bar button,.btn{{background:var(--accent);color:#fff;border:none;padding:.3rem .8rem;border-radius:4px;cursor:pointer;font-size:.8rem}}
.filter-bar button:hover,.btn:hover{{filter:brightness(1.08)}}
.filter-bar button:disabled,.btn:disabled{{opacity:.4;cursor:default}}
/* Inline clear (×) for applied searchable filters */
.flt-search{{position:relative;flex:1;min-width:140px}}
.flt-search input{{width:100%;padding-right:28px!important}}
.filter-bar .flt-clear{{
  position:absolute;right:4px;top:50%;transform:translateY(-50%);
  border:none;background:transparent!important;color:var(--fg2)!important;
  cursor:pointer;padding:2px 6px!important;font-size:13px;line-height:1;
  border-radius:3px;filter:none!important;z-index:2;
}}
.filter-bar .flt-clear:hover{{color:var(--fg)!important;background:rgba(0,0,0,.06)!important}}
/* Searchable org filter — same chrome as <select>, with type-to-filter */
.org-combo{{position:relative;min-width:160px;max-width:280px;flex:0 1 280px}}
.org-combo.wide{{max-width:420px;flex:1 1 280px;min-width:200px}}
.org-combo input{{
  width:100%;padding:7px 28px 7px 10px;border:2px solid var(--border);border-radius:6px;
  font-size:13px;background-color:var(--card);color:var(--fg);
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%2357606a' d='M2.5 4.5L6 8.2l3.5-3.7H2.5z'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 8px center;background-size:12px 12px;
  appearance:none;-webkit-appearance:none;
}}
.org-combo.has-value input{{padding-right:46px;background-position:right 8px center}}
.org-combo .flt-clear{{right:22px}}
.org-combo input:focus{{outline:none;border-color:var(--accent)}}
.org-combo-list{{display:none;position:absolute;z-index:80;left:0;right:0;top:calc(100% + 2px);max-height:240px;overflow:auto;background:var(--card);border:2px solid var(--border);border-radius:6px;box-shadow:var(--shadow)}}
.org-combo-list.open{{display:block}}
.org-combo-opt{{padding:6px 10px;cursor:pointer;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.org-combo-opt:hover,.org-combo-opt.active{{background:rgba(15,52,96,.08)}}
.org-combo-opt.hidden{{display:none}}
.org-combo-empty{{padding:6px 10px;color:var(--fg2);font-size:.82rem}}
.org-combo-empty.hidden{{display:none}}

/* ── Drill panel ── */
.drill{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:1rem;box-shadow:var(--shadow);margin:.6rem 0}}
.drill .back-btn{{background:none;border:1px solid var(--border);padding:4px 12px;border-radius:4px;cursor:pointer;font-size:.82rem;margin-bottom:.8rem}}.drill .back-btn:hover{{background:var(--code-bg)}}

/* ── Status badges ── */
.st{{display:inline-block;font-size:.68rem;font-weight:700;padding:2px 7px;border-radius:10px;text-transform:uppercase;letter-spacing:.3px}}
.st-c{{background:var(--pass-bg);color:var(--pass)}}.st-f{{background:var(--fail-bg);color:var(--fail)}}
.st-s{{background:var(--skip-bg);color:var(--skip)}}.st-p{{background:var(--skip-bg);color:var(--skip)}}
.st-fc{{background:var(--warn-bg);color:var(--warn)}}
.obj-tbl tr.row-fc{{background:var(--warn-bg)}}
.obj-tbl td.err{{font-size:.78rem;color:var(--fail);max-width:350px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.obj-tbl td.note-fc{{color:var(--warn)}}
.obj-tbl tr.row-f{{background:#fff5f5}}.obj-tbl tr.row-s{{background:var(--skip-bg)}}.obj-tbl tr.row-p{{background:var(--skip-bg)}}
.obj-tbl tr{{cursor:pointer}}.obj-tbl tr:hover{{background:#e8f0fe}}
/* ── Field comparison panel ── */
.fd-panel{{padding:.5rem;background:#f8f9fa;border:1px solid var(--border);border-radius:6px;margin:.3rem 0}}
.fd-tbl{{width:100%;font-size:.78rem;border-collapse:collapse}}
.fd-tbl th{{text-align:left;padding:4px 8px;background:#e1e4e8;font-weight:600;font-size:.72rem;text-transform:uppercase}}
.fd-tbl td{{padding:4px 8px;border-bottom:1px solid #eee;vertical-align:top}}
.fd-tbl .fd-fn{{font-weight:600;color:var(--fg);white-space:nowrap;width:180px}}
.fd-tbl .fd-v{{font-family:monospace;font-size:.72rem;word-break:break-all;max-width:350px}}
.fd-tbl tr.fd-ok{{background:var(--pass-bg)}}.fd-tbl tr.fd-diff{{background:var(--warn-bg)}}
.fd-row td{{padding:0!important;border:none!important}}

.footer{{text-align:center;padding:1.2rem;color:#8b949e;font-size:.76rem;border-top:1px solid var(--border);margin-top:1.5rem}}
@media(max-width:768px){{
  .container{{padding:.8rem}}.shell{{padding:0 .8rem}}.cards{{grid-template-columns:repeat(2,1fr)}}
  .tab-btn{{padding:.4rem .6rem;font-size:.76rem}}
  .banner{{padding:.6rem 0}}.banner .shell{{flex-wrap:wrap}}.funnel-label{{width:80px}}
  .pbar-name{{width:120px}}.pbar-nums{{width:120px}}
}}
</style>
</head>
<body>
{mock_banner}
<div class="banner{verdict_class}">
  <div class="shell">
  <div class="verdict-badge">{verdict_icon} {escape(es.verdict)}</div>
  <div class="banner-info">
    <div class="banner-title">AAP 2.4 &rarr; 2.6 Migration Validation</div>
    <div class="banner-meta">
      <span>{escape(meta.run_id)}</span>
      <span>{source_host} &rarr; {target_host}</span>
      <span>{_fmt(total_source)} objects &middot; {n_types} types &middot; {n_orgs} orgs</span>
      {scope_banner}
    </div>
  </div>
  </div>
</div>

<nav>
  <div class="shell">
  <button class="tab-btn active" data-tab="dashboard">Dashboard</button>
  <button class="tab-btn" data-tab="orgs">Org Health <span class="ct{' ok' if (orgs_red + orgs_amber) == 0 else ''}" title="Organizations with red or amber health">{_fmt(orgs_red + orgs_amber)}</span></button>
  <button class="tab-btn" data-tab="types">Resource Types <span style="opacity:.55;font-weight:600">T1</span> <span class="ct{' ok' if types_failed == 0 else ''}" title="Resource types with failures">{_fmt(types_failed)}</span></button>
  <button class="tab-btn" data-tab="missing">Missing <span style="opacity:.55;font-weight:600">T2</span> <span class="ct{' ok' if total_missing == 0 else ''}">{_fmt(total_missing)}</span></button>
  <button class="tab-btn" data-tab="extra">Extra <span style="opacity:.55;font-weight:600">T2</span>{f' <span class="ct{" ok" if total_extra_listed == 0 else " warn"}" title="Target objects not matched to export (hosts omitted from list)">{_fmt(total_extra_listed)}</span>' if is_live_validate else ''}</button>
  <button class="tab-btn" data-tab="syncs">Syncs <span style="opacity:.55;font-weight:600">T2</span>{f' <span class="ct{" ok" if total_sync_failed == 0 else ""}" title="Failed project or inventory source syncs">{_fmt(total_sync_failed)}</span>' if is_live_validate else ''}</button>
  <button class="tab-btn" data-tab="fields">Field Changes <span style="opacity:.55;font-weight:600">T3</span>{f' <span class="ct{" ok" if total_field_mm == 0 else " warn"}" title="Objects with ≥1 field mismatch">{_fmt(total_field_mm)}</span>' if is_live_validate else ''}</button>
  <button class="tab-btn" data-tab="hosts">Hosts <span style="opacity:.55;font-weight:600">T4</span>{f' <span class="ct{" ok" if host_issues == 0 else ""}">{_fmt(host_issues)}</span>' if host_tab_ran else ""}</button>
  <button class="tab-btn" data-tab="auditor">Auditor{auditor_tab_badge}</button>
  <button class="tab-btn" data-tab="method">Methodology</button>
  </div>
</nav>

<div class="container">
{scope_callout}
<!-- Lazy-rendered tabs -->
<div class="content" id="dashboardContent"></div>
<div class="content hidden" id="orgsContent"></div>
<div class="content hidden" id="typesContent"></div>
<div class="content hidden" id="missingContent"></div>
<div class="content hidden" id="extraContent"></div>
<div class="content hidden" id="syncsContent"></div>
<div class="content hidden" id="fieldsContent"></div>

<!-- Hosts tab: client-rendered (mismatch default + inventory search) -->
<div class="content hidden" id="hostsContent"></div>

<div class="content hidden" id="auditorContent">
<h2>Auditor Verification &mdash; Gateway Platform Auditor</h2>
<div class="callout callout-info"><strong>Why:</strong> On AAP 2.6, <code>is_system_auditor</code> is Gateway-synced. This verifies the Gateway role directly.</div>
<div class="cards" style="grid-template-columns:repeat(4,1fr);">
  <div class="card"><div class="v">{_fmt(ac.source_system_auditors)}</div><div class="l">Source auditors</div></div>
  <div class="card"><div class="v">{_fmt(ac.gateway_platform_auditors)}</div><div class="l">Gateway auditors</div></div>
  <div class="card"><div class="v{' ok' if ac.mismatches == 0 else ' bad'}">{_fmt(ac.mismatches)}</div><div class="l">Mismatches</div></div>
  <div class="card"><div class="v">{len(ac.details)}</div><div class="l">Checked</div></div>
</div>
<table><thead><tr><th>Username</th><th>Gateway Platform Auditor</th><th>Verdict</th></tr></thead>
<tbody>{aud_html}</tbody></table>
</div>

<div class="content hidden" id="methodContent">
<h2>Methodology</h2>
<div class="callout callout-pass"><strong>Read-Only:</strong> {_fmt(meta.total_api_calls)} API calls, all GET. Zero writes.</div>
<h3>Tiers</h3>
<table>
  <tr><th>Tier</th><th>Scope</th><th>Coverage</th></tr>
  <tr><td><strong>T1</strong></td><td>Counts</td><td>{t1_coverage}</td></tr>
  <tr><td><strong>T2</strong></td><td>Existence</td><td>{t2_coverage}</td></tr>
  <tr><td><strong>T3</strong></td><td>Field parity</td><td>{t3_coverage}</td></tr>
  <tr><td><strong>T4</strong></td><td>Host sampling</td><td>{t4_coverage}</td></tr>
</table>
<div class="callout callout-info" style="margin-top:.6rem"><strong>Extra on target:</strong> Objects present on AAP 2.6 with no identity match to an exported source object for this run. Often objects created or changed on the target outside the migration set. Hosts are counted in T1/T2 but listed under Hosts (T4), not the Extra tab. With <code>--orgs</code>, the list is limited to the scoped organization(s).</div>
<div class="callout callout-info" style="margin-top:.6rem"><strong>T4 confidence:</strong> {confidence_note}</div>
<h3>Comparison Rules (v{escape(meta.comparison_rules_version)})</h3>
<table>
  <tr><th>#</th><th>Rule</th><th>Effect</th></tr>
  <tr><td>1</td><td>METADATA_FIELDS ({exc.metadata_fields})</td><td>Excluded: id, type, url, related, summary_fields, opa_query_path, local_path, client_id, capacity, jobs_total, inventory_sources_with_failures, …</td></tr>
  <tr><td>2</td><td>COMPUTED_FIELDS ({exc.computed_fields})</td><td>Excluded: next_run, status, last_job_run</td></tr>
  <tr><td>3</td><td>RELATED_COLLECTIONS ({exc.related_collections})</td><td>Excluded: credentials, schedules, survey_spec</td></tr>
  <tr><td>4</td><td>FK_FIELDS ({exc.fk_fields_by_name})</td><td>Compared by referent name, not raw ID</td></tr>
  <tr><td>5</td><td>ENCRYPTED</td><td>$encrypted$ = not comparable</td></tr>
  <tr><td>6</td><td>NONE_VS_DICT</td><td>None = {{key: None, ...}}</td></tr>
  <tr><td>7</td><td>CRED_TYPE</td><td>inputs, injectors excluded</td></tr>
  <tr><td>8</td><td>SCHED_ENABLED</td><td>enabled excluded</td></tr>
  <tr><td>9</td><td>AUDITOR</td><td>{auditor_rule_effect}</td></tr>
  <tr><td>10</td><td>PRIVATE</td><td>_ prefixed fields excluded</td></tr>
</table>
<h3>Run Parameters</h3>
<div class="meta-grid">
  <div class="k">Started</div><div class="v">{escape(meta.started_at)}</div>
  <div class="k">Completed</div><div class="v">{escape(meta.completed_at)}</div>
  <div class="k">Source</div><div class="v">{source_host}</div>
  <div class="k">Target</div><div class="v">{target_host}</div>
  <div class="k">Tiers</div><div class="v">{escape(tiers_str)}</div>
  <div class="k">API calls</div><div class="v">{_fmt(meta.total_api_calls)}</div>
  <div class="k">Mode</div><div class="v">{escape(meta.mode)}</div>
  <div class="k">Org scope</div><div class="v">{escape(org_scope_method)}</div>
  <div class="k">Field data source</div><div class="v">{escape(fd_source_label)}</div>
  <div class="k">Host sample</div><div class="v">{_fmt(meta.host_sample_size)} (seed {meta.host_sample_seed})</div>
  <div class="k">Overrides</div><div class="v">{escape(type_overrides_str)}</div>
</div>
</div>
</div>

<div class="footer">
  <code>aap-bridge validate</code> &middot; Rules v{escape(meta.comparison_rules_version)} &middot; {escape(meta.run_id)} &middot; 0600
</div>

<script>
const D={json_block};
const INV={inv_block};
const FD={fd_block};
const HOST_T4={host_t4_block};
const SCOPED_ORGS={json.dumps(scoped_orgs)};
let curTab='dashboard';

/* ── Helpers ── */
function esc(s){{if(s==null)return'';var d=document.createElement('div');d.textContent=String(s);return d.innerHTML}}
function fmt(n){{return n.toLocaleString()}}
function ids(s,t){{return'<span class="ids">[src:'+(s!=null?s:'N/A')+' → tgt:'+(t!=null?t:'N/A')+']</span>'}}
function objD(n,o,s,t){{var h='<strong>'+esc(n)+'</strong>';if(o)h+=' &middot; '+esc(o);h+=' '+ids(s,t);return h}}
function pct(n,d){{return d?Math.round(n/d*100):0}}
/* Match/completion %: never round up to 100% when anything is still missing */
function pctMatched(matched,source,gaps){{
  if(!source)return 0;
  if(gaps>0){{
    if(matched<=0)return 0;
    return Math.min(99,Math.floor(matched/source*100));
  }}
  return matched>=source?100:Math.floor(matched/source*100);
}}
/* Largest-remainder so parts always sum to 100 (or 0 if total is 0) */
function pctParts(parts,total){{
  if(!total)return parts.map(function(){{return 0}});
  var exact=parts.map(function(p){{return p/total*100}});
  var floors=exact.map(function(x){{return Math.floor(x)}});
  var rem=100-floors.reduce(function(a,b){{return a+b}},0);
  var order=exact.map(function(x,i){{return{{i:i,frac:x-floors[i]}}}})
    .sort(function(a,b){{return b.frac-a.frac||a.i-b.i}});
  for(var k=0;rem>k;k++)floors[order[k%order.length].i]++;
  return floors;
}}
function hcls(h){{return h==='red'?'h-r':h==='amber'?'h-a':'h-g'}}
function hlbl(h){{return h.toUpperCase()}}
function allTypes(){{return D.per_type.map(function(t){{return t.resource_type}}).sort()}}
function allOrgNames(){{
  var names=Object.keys(D.per_org||{{}});
  if(!names.length){{
    var seen={{}};
    Object.keys(INV||{{}}).forEach(function(rt){{
      (INV[rt]||[]).forEach(function(e){{if(e.o)seen[e.o]=1}});
    }});
    names=Object.keys(seen);
  }}
  return names.sort(function(a,b){{return a.localeCompare(b)}});
}}
/* Full re-renders replace inputs; remember caret so typing does not lose focus */
var _keepFocus=null;
function keepFocus(el){{
  if(!el||!el.id)return;
  _keepFocus={{id:el.id,s:el.selectionStart,e:el.selectionEnd}};
}}
function restoreKeptFocus(){{
  if(!_keepFocus)return;
  var el=document.getElementById(_keepFocus.id);
  if(el){{
    el.focus();
    try{{el.setSelectionRange(_keepFocus.s,_keepFocus.e)}}catch(x){{}}
  }}
  _keepFocus=null;
}}
/* Shared list-tab pagination sizes */
var PAGE_MISSING=50,PAGE_EXTRA=50,PAGE_SYNC=50,PAGE_FIELDS=20,PAGE_OBJECTS=100;
function cmpLocale(a,b){{return String(a||'').localeCompare(String(b||''));}}
function paginateSlice(page,perPage,total){{
  var pages=Math.ceil(total/perPage)||1;
  page=clampPage(page,pages);
  var start=(page-1)*perPage;
  return {{page:page,pages:pages,start:start,sliceStart:start,sliceEnd:start+perPage}};
}}
function showTotalSuffix(filteredCount,poolCount,totalCount,totalLabel){{
  if(totalCount==null)return '';
  if(totalCount!==poolCount||(filteredCount<poolCount&&poolCount<totalCount)){{
    return ' &middot; '+fmt(totalCount)+(totalLabel?' '+totalLabel:'')+' total';
  }}
  return '';
}}
function showingLine(filteredCount,poolCount,unit,totalCount){{
  var suffix=unit?' '+unit:'';
  var line='Showing '+fmt(filteredCount)+' of '+fmt(poolCount)+suffix;
  line+=showTotalSuffix(filteredCount,poolCount,totalCount);
  return '<div style="font-size:.82rem;color:var(--fg2);margin:.3rem 0">'+line+'</div>';
}}
function renderPager(page,pages,total,prevExpr,nextExpr,unit){{
  if(pages<=1)return'';
  var h='<div class="pager">';
  h+='<button onclick="'+prevExpr+'"'+(page<=1?' disabled':'')+'>&#9664; Prev</button>';
  h+='<span class="pg-info">Page '+page+' of '+pages+' ('+total+' '+(unit||'results')+')</span>';
  h+='<button onclick="'+nextExpr+'"'+(page>=pages?' disabled':'')+'>Next &#9654;</button>';
  h+='</div>';
  return h;
}}
function sortByNameTypeOrg(a,b,sortKey){{
  if(sortKey==='name')return cmpLocale(a.name,b.name);
  if(sortKey==='type')return cmpLocale(a.type,b.type)||cmpLocale(a.name,b.name);
  if(sortKey==='org')return cmpLocale(a.org,b.org)||cmpLocale(a.name,b.name);
  return 0;
}}
function sortById(a,b,sortKey){{
  if(sortKey==='sid')return (a.sid||0)-(b.sid||0)||cmpLocale(a.name,b.name);
  if(sortKey==='tid')return (a.tid||0)-(b.tid||0)||cmpLocale(a.name,b.name);
  return 0;
}}
function jumpToTab(tab,opts){{
  opts=opts||{{}};
  if(tab==='missing'){{
    misType=opts.type||'all';misOrg=opts.org||'';misSearch='';
    misExplReason='';misExplClass=opts.explClass||'';misPage=1;
  }}else if(tab==='extra'){{
    extraType=opts.type||'all';extraOrg=opts.org||'';extraSearch='';extraPage=1;
  }}else if(tab==='fields'){{
    fldType=opts.type||'all';fldOrg=opts.org||'';fldSearch='';
    fldField=opts.field||'all';fldPage=1;
  }}
  switchTab(tab);
}}
/* Searchable combobox — click opens list, type to filter, pick to apply */
var _orgComboCbs={{}};
function comboFilterHtml(id,value,varName,thenExpr,names,opts){{
  opts=opts||{{}};
  var placeholder=opts.placeholder||'All';
  var allLabel=opts.allLabel||placeholder;
  var emptyMsg=opts.emptyMsg||'No matching options';
  var clearTitle=opts.clearTitle||'Clear filter';
  var wide=!!opts.wide;
  _orgComboCbs[id]={{varName:varName,then:thenExpr}};
  var has=!!value;
  var h='<div class="org-combo'+(wide?' wide':'')+(has?' has-value':'')+'" id="'+id+'-wrap">';
  h+='<input id="'+id+'" type="text" placeholder="'+esc(placeholder)+'" value="'+esc(value||'')+'" autocomplete="off" ';
  h+='onfocus="orgComboOpen(\\''+id+'\\')" ';
  h+='oninput="orgComboFilter(\\''+id+'\\')" ';
  h+='onkeydown="orgComboKey(event,\\''+id+'\\')">';
  if(has){{
    h+='<button type="button" class="flt-clear" title="'+esc(clearTitle)+'" ';
    h+='onclick="event.preventDefault();event.stopPropagation();orgComboPick(\\''+id+'\\',\\'\\')">&#10005;</button>';
  }}
  h+='<div class="org-combo-list" id="'+id+'-list">';
  h+='<div class="org-combo-opt" data-v="" onmousedown="event.preventDefault();orgComboPick(\\''+id+'\\',\\'\\')">'+esc(allLabel)+'</div>';
  names.forEach(function(n){{
    var sel=value===n?' active':'';
    h+='<div class="org-combo-opt'+sel+'" data-v="'+esc(n)+'" onmousedown="event.preventDefault();orgComboPick(\\''+id+'\\',this.dataset.v)">'+esc(n)+'</div>';
  }});
  h+='<div class="org-combo-empty hidden">'+esc(emptyMsg)+'</div>';
  h+='</div></div>';
  return h;
}}
function orgFilterHtml(id,value,varName,thenExpr){{
  return comboFilterHtml(id,value,varName,thenExpr,allOrgNames(),{{
    placeholder:'All orgs',
    allLabel:'All orgs',
    emptyMsg:'No matching organizations',
    clearTitle:'Clear organization filter'
  }});
}}
function reasonFilterHtml(id,value,varName,thenExpr,reasons){{
  return comboFilterHtml(id,value,varName,thenExpr,reasons,{{
    placeholder:'All explanations',
    allLabel:'All explanations',
    emptyMsg:'No matching explanations',
    clearTitle:'Clear explanation filter',
    wide:true
  }});
}}
function searchFilterHtml(id,value,placeholder,onInputExpr,clearExpr,extraStyle,listId){{
  var h='<div class="flt-search"'+(extraStyle?' style="'+extraStyle+'"':'')+'>';
  h+='<input id="'+id+'" type="text" placeholder="'+esc(placeholder)+'" value="'+esc(value||'')+'" autocomplete="off" ';
  if(listId)h+='list="'+listId+'" ';
  h+='oninput="keepFocus(this);'+onInputExpr+'">';
  if(value){{
    h+='<button type="button" class="flt-clear" title="Clear filter" onclick="event.preventDefault();'+clearExpr+'">&#10005;</button>';
  }}
  h+='</div>';
  return h;
}}
function orgComboCloseAll(exceptId){{
  document.querySelectorAll('.org-combo-list.open').forEach(function(list){{
    if(exceptId&&list.id===exceptId+'-list')return;
    list.classList.remove('open');
  }});
}}
function orgComboOpen(id){{
  orgComboCloseAll(id);
  var list=document.getElementById(id+'-list');
  var input=document.getElementById(id);
  if(!list||!input)return;
  list.classList.add('open');
  orgComboFilter(id);
  // Select text so typing replaces current selection quickly
  try{{input.select()}}catch(x){{}}
}}
function orgComboToggle(id){{
  var list=document.getElementById(id+'-list');
  if(!list)return;
  if(list.classList.contains('open')){{list.classList.remove('open');return}}
  orgComboOpen(id);
  var input=document.getElementById(id);
  if(input)input.focus();
}}
function orgComboFilter(id){{
  var input=document.getElementById(id);
  var list=document.getElementById(id+'-list');
  if(!input||!list)return;
  list.classList.add('open');
  var q=(input.value||'').toLowerCase();
  var shown=0;
  list.querySelectorAll('.org-combo-opt').forEach(function(o){{
    var v=o.getAttribute('data-v')||'';
    var label=(o.textContent||'').toLowerCase();
    var show=!q||v===''||label.indexOf(q)>=0;
    o.classList.toggle('hidden',!show);
    if(show&&v!=='')shown++;
  }});
  var empty=list.querySelector('.org-combo-empty');
  if(empty)empty.classList.toggle('hidden',!q||shown>0);
}}
function orgComboPick(id,value){{
  var cb=_orgComboCbs[id];
  if(!cb)return;
  globalThis[cb.varName]=value||'';
  Function(cb.then)();
}}
function orgComboKey(ev,id){{
  var list=document.getElementById(id+'-list');
  var input=document.getElementById(id);
  if(!list||!input)return;
  if(ev.key==='Escape'){{list.classList.remove('open');input.blur();return}}
  if(ev.key==='ArrowDown'||ev.key==='ArrowUp'){{
    ev.preventDefault();
    list.classList.add('open');
    var opts=Array.prototype.slice.call(list.querySelectorAll('.org-combo-opt:not(.hidden)'));
    if(!opts.length)return;
    var cur=list.querySelector('.org-combo-opt.active:not(.hidden)');
    var idx=cur?opts.indexOf(cur):-1;
    if(ev.key==='ArrowDown')idx=Math.min(opts.length-1,idx+1);
    else idx=Math.max(0,idx-1);
    opts.forEach(function(o){{o.classList.remove('active')}});
    opts[idx].classList.add('active');
    opts[idx].scrollIntoView({{block:'nearest'}});
    return;
  }}
  if(ev.key==='Enter'){{
    ev.preventDefault();
    var active=list.querySelector('.org-combo-opt.active:not(.hidden)');
    if(active){{orgComboPick(id,active.getAttribute('data-v')||'');return}}
    var q=(input.value||'').toLowerCase();
    if(!q){{orgComboPick(id,'');return}}
    var opts=Array.prototype.slice.call(list.querySelectorAll('.org-combo-opt:not(.hidden)'));
    var exact=opts.filter(function(o){{return(o.getAttribute('data-v')||'').toLowerCase()===q}});
    if(exact.length){{orgComboPick(id,exact[0].getAttribute('data-v')||'');return}}
    var named=opts.filter(function(o){{return(o.getAttribute('data-v')||'')!==''}});
    if(named.length===1){{orgComboPick(id,named[0].getAttribute('data-v')||'');return}}
  }}
}}
function stBadge(st,fc){{
  var m={{c:'COMPLETED',f:'FAILED',s:'SKIPPED',p:'PENDING'}};
  var h='<span class="st st-'+st+'">'+((m[st])||st)+'</span>';
  if(fc)h+=' <span class="st st-fc" title="One or more fields differ from source">FIELDS CHANGED</span>';
  return h;
}}
function stLabel(st){{var m={{c:'Completed',f:'Failed',s:'Skipped',p:'Pending'}};return m[st]||st}}
function invForType(rt){{return INV[rt]||[]}}
function invForTypeOrg(rt,org){{return(INV[rt]||[]).filter(function(e){{return e.o===org}})}}
function explClass(expl){{
  var e=expl||'';
  // Prefer Failed/Skipped tokens (including live-wrapped forms) before bare live.
  if(/^Failed/i.test(e)||/\\(Failed/i.test(e))return'failed';
  if(/^Skipped/i.test(e)||/\\(Skipped/i.test(e))return'skipped';
  if(/Pending migration/i.test(e))return'pending';
  if(/Not found on live target/i.test(e))return'live';
  return'unexplained';
}}
function matchesObjFilter(e){{
  if(objSt==='issues')return e.st==='f'||e.st==='p'||e.st==='s'||!!e.fc;
  if(objSt==='fc')return !!e.fc;
  if(objSt!=='all'&&e.st!==objSt)return false;
  return true;
}}
function countIssueObjects(items){{
  return items.filter(function(e){{
    return e.st==='f'||e.st==='p'||e.st==='s'||!!e.fc;
  }}).length;
}}
function typeMigrationBucket(t){{
  return t.migration_bucket||'p';
}}
function clampPage(page,pages){{
  if(!pages||pages<1)return 1;
  if(page>pages)return pages;
  if(page<1)return 1;
  return page;
}}
function seedScopedOrgOnce(flagName,getter,setter){{
  if(globalThis[flagName])return;
  globalThis[flagName]=true;
  if(SCOPED_ORGS&&SCOPED_ORGS.length===1&&!getter())setter(SCOPED_ORGS[0]);
}}
function sortProblemsFirst(arr){{
  return arr.slice().sort(function(a,b){{
    var ra=a.st==='f'?0:a.st==='p'?1:a.fc?2:a.st==='s'?3:4;
    var rb=b.st==='f'?0:b.st==='p'?1:b.fc?2:b.st==='s'?3:4;
    if(ra!==rb)return ra-rb;
    return String(a.n||'').localeCompare(String(b.n||''));
  }});
}}
function truncVal(v){{
  var s=v==null?'—':String(v);
  return s.length>300?s.substring(0,300)+'…':s;
}}

/* ── Field comparison ── */
var fdExpanded=null;
function fieldCompareHtml(rt,sid,tid){{
  var td=FD[rt];
  if(!td)return'<div class="callout callout-warn">No field data available for '+esc(rt)+'</div>';
  var cols=td.c,src=td.s[sid],tgt=td.t[sid]||null;
  if(!src&&!tgt)return'<div class="callout callout-warn">No field data found for src:'+sid+'</div>';
  var h='<table class="fd-tbl"><thead><tr><th>Field</th><th>Source (2.4)</th><th>Target (2.6)</th><th></th></tr></thead><tbody>';
  cols.forEach(function(c,i){{
    var sv=src?src[i]:null,tv=tgt?tgt[i]:null;
    var ss=sv!=null?JSON.stringify(sv):'—';
    var ts=tv!=null?JSON.stringify(tv):'—';
    var match=(ss===ts);
    var cls=tgt==null?'':match?'fd-ok':'fd-diff';
    h+='<tr class="'+cls+'">';
    h+='<td class="fd-fn">'+esc(c)+'</td>';
    h+='<td class="fd-v">'+esc(ss.length>200?ss.substring(0,200)+'…':ss)+'</td>';
    h+='<td class="fd-v">'+esc(ts.length>200?ts.substring(0,200)+'…':ts)+'</td>';
    h+='<td>'+(tgt==null?'':'<span class="st st-'+(match?'c':'fc')+'">'+(match?'✓':'≠')+'</span>')+'</td>';
    h+='</tr>';
  }});
  h+='</tbody></table>';
  return h;
}}
function toggleObjectDetail(rt,idx,rowEl){{
  var key='od:'+rt+':'+idx;
  var next=rowEl.nextElementSibling;
  if(next&&next.classList.contains('fd-row')){{next.remove();fdExpanded=null;return;}}
  var old=document.querySelector('.fd-row');if(old)old.remove();
  var items=INV[rt]||[];var e=items[idx];
  if(!e)return;
  var h='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.6rem .8rem;font-size:.82rem;margin-bottom:.6rem">';
  h+='<div><span style="color:var(--fg2)">Name:</span> <strong>'+esc(e.n)+'</strong></div>';
  h+='<div><span style="color:var(--fg2)">Type:</span> '+esc(rt)+'</div>';
  h+='<div><span style="color:var(--fg2)">Source ID:</span> '+(e.s!=null?e.s:'—')+'</div>';
  h+='<div><span style="color:var(--fg2)">Target ID:</span> '+(e.t!=null?e.t:'—')+'</div>';
  if(e.o)h+='<div><span style="color:var(--fg2)">Organization:</span> '+esc(e.o)+'</div>';
  h+='<div><span style="color:var(--fg2)">Status:</span> '+stBadge(e.st,e.fc)+'</div>';
  if(e.fc)h+='<div style="grid-column:1/-1"><span style="color:var(--fg2)">Field parity:</span> <span style="color:var(--warn);font-weight:600">One or more fields differ from source (see comparison below)</span></div>';
  if(e.e)h+='<div style="grid-column:1/-1"><span style="color:var(--fg2)">Error:</span> <span style="color:var(--fail)">'+esc(e.e)+'</span></div>';
  h+='</div>';
  var td=FD[rt];
  if(td){{h+=fieldCompareHtml(rt,e.s,e.t);}}
  else{{h+='<div style="font-size:.78rem;color:var(--fg2);font-style:italic">Field comparison not available in database-only mode. Re-run with <code>--live</code> to compare field values.</div>';}}
  var tr=document.createElement('tr');tr.className='fd-row';
  var tc=document.createElement('td');tc.colSpan=20;
  tc.innerHTML='<div class="fd-panel">'+h+'</div>';
  tr.appendChild(tc);
  rowEl.parentNode.insertBefore(tr,rowEl.nextSibling);
  fdExpanded=key;
}}

/* ── Tab switching ── */
function init(){{
  renderDashboard();
  document.querySelectorAll('.tab-btn').forEach(function(b){{
    b.addEventListener('click',function(){{switchTab(b.dataset.tab)}});
  }});
  document.addEventListener('click',function(e){{
    if(!e.target.closest||!e.target.closest('.org-combo'))orgComboCloseAll();
  }});
}}
function switchTab(tab){{
  curTab=tab;
  document.querySelectorAll('.tab-btn').forEach(function(b){{b.classList.toggle('active',b.dataset.tab===tab)}});
  var lazy=['dashboardContent','orgsContent','typesContent','missingContent','extraContent','syncsContent','fieldsContent','hostsContent'];
  document.querySelectorAll('.content').forEach(function(c){{
    var on=c.id===tab+'Content';
    c.classList.toggle('hidden',!on);
    // Drop inactive lazy tab DOM so filter input IDs stay unique
    if(!on&&lazy.indexOf(c.id)>=0)c.innerHTML='';
  }});
  if(tab==='dashboard')renderDashboard();
  else if(tab==='orgs')renderOrgs();
  else if(tab==='types')renderTypes();
  else if(tab==='missing')renderMissing();
  else if(tab==='extra')renderExtra();
  else if(tab==='syncs')renderSyncs();
  else if(tab==='fields')renderFields();
  else if(tab==='hosts')renderHosts();
}}
function jumpToMissing(typeFilter,orgFilter){{
  jumpToTab('missing',{{type:typeFilter,org:orgFilter}});
}}
function jumpToUnexplained(){{
  jumpToTab('missing',{{explClass:'unexplained'}});
}}
function jumpToExtra(typeFilter,orgFilter){{
  jumpToTab('extra',{{type:typeFilter,org:orgFilter}});
}}
function jumpToFields(typeFilter,orgFilter){{
  jumpToTab('fields',{{type:typeFilter,org:orgFilter}});
}}
function jumpToSyncs(){{
  syncShow='failed';syncType='all';syncOrg='';syncSearch='';syncPage=1;
  switchTab('syncs');
}}
function jumpToFailedTypes(){{
  allObjView=null;typeDrill=null;typesBucketFilter='f';switchTab('types');
}}
function jumpToAllObjects(statusFilter){{
  allObjView=true;typeDrill=null;objSt=statusFilter||'issues';allObjType='all';objOrg='';objSearch='';objPage=1;switchTab('types');
}}
function jumpToTypeObject(typeName,objName,orgName){{
  allObjView=null;
  typeDrill=typeName||null;
  objSt='all';
  objOrg=orgName||'';
  objSearch=objName||'';
  objPage=1;
  switchTab('types');
}}

/* ── Tab 1: Dashboard ── */
function renderDashboard(){{
  var pt=D.per_type,po=D.per_org||{{}};
  var src=0,tgt=0,mat=0,mis=0,fmm=0,expl=0,unex=0;
  pt.forEach(function(t){{
    src+=t.t1_counts.source;tgt+=t.t1_counts.target;
    mat+=t.t2_existence.matched;mis+=t.t2_existence.missing_on_target;
    fmm+=t.t3_field_parity.mismatching;
    expl+=t.t1_counts.explained_failures+t.t1_counts.explained_skips;
    unex+=t.t1_counts.unexplained;
  }});
  var okeys=Object.keys(po),nOrgs=okeys.length;
  var oR=0,oA=0,oG=0;
  okeys.forEach(function(k){{var h=po[k].health;if(h==='red')oR++;else if(h==='amber')oA++;else oG++}});
  var typesFailed=0;
  pt.forEach(function(t){{if(typeMigrationBucket(t)==='f')typesFailed++;}});

  var h='<h2>Migration Overview</h2>';

  // Partition bars: Matched + Explained Gaps + Unexplained Gaps always sum to 100%
  h+='<div class="funnel">';
  var partTotal=mat+expl+unex;
  if(!partTotal)partTotal=src;
  var parts=pctParts([mat,expl,unex],partTotal);
  var steps=[
    ['Source Objects',src,src?100:0,'var(--accent)'],
    ['Matched',mat,parts[0],'var(--pass)'],
    ['Explained Gaps',expl,parts[1],'var(--skip)'],
    ['Unexplained Gaps',unex,parts[2],'var(--fail)']
  ];
  steps.forEach(function(s){{
    var count=s[1],w=s[2];
    h+='<div class="funnel-step">';
    h+='<div class="funnel-label">'+s[0]+'</div>';
    h+='<div class="funnel-track">';
    if(count>0&&w>0){{
      h+='<div class="funnel-bar" style="width:'+w+'%;background:'+s[3]+'" title="'+fmt(count)+' ('+w+'%)"></div>';
    }}else if(count>0){{
      // Non-zero count rounded to 0% — show a hairline so it is not invisible
      h+='<div class="funnel-bar" style="width:1px;background:'+s[3]+'" title="'+fmt(count)+' (<1%)"></div>';
    }}
    h+='</div>';
    h+='<div class="funnel-count">'+fmt(count)+' · '+w+'%</div>';
    h+='</div>';
  }});
  h+='</div>';
  if(partTotal&&(mat+expl+unex)===partTotal){{
    h+='<div style="font-size:.75rem;color:var(--fg2);margin:-.4rem 0 .6rem">Matched + Explained Gaps + Unexplained Gaps = 100% of source objects</div>';
  }}

  // Stat cards — green=success, yellow=field changes/extras, red=problems
  var extListed=0;
  pt.forEach(function(t){{
    var e=t.t2_existence||{{}};
    if(t.resource_type==='hosts')return;
    extListed+=(e.extra_details||[]).length+(e.extra_truncated_count||0);
  }});
  h+='<div class="cards">';
  h+='<div class="card" style="cursor:pointer" onclick="jumpToAllObjects(\\'all\\')"><div class="v">'+fmt(src)+'</div><div class="l">Source objects &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="jumpToAllObjects(\\'all\\')" title="Target-side object count from T1 (browse source inventory via All Objects)"><div class="v">'+fmt(tgt)+'</div><div class="l">Target objects &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="jumpToAllObjects(\\'c\\')" title="T2 matched count; opens Completed inventory rows"><div class="v ok">'+fmt(mat)+'</div><div class="l">Matched &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="jumpToMissing()"><div class="v'+(mis===0?' ok':' bad')+'">'+fmt(mis)+'</div><div class="l">Missing &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="jumpToFailedTypes()" title="Resource types with missing objects or import failures"><div class="v'+(typesFailed===0?' ok':' bad')+'">'+fmt(typesFailed)+'</div><div class="l">Failed resource types &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="jumpToExtra()" title="Listed extras exclude hosts (see Extra tab)"><div class="v'+(extListed===0?' ok':' warn')+'">'+fmt(extListed)+'</div><div class="l">Extra on target &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="jumpToFields()"><div class="v'+(fmm===0?' ok':' warn')+'">'+fmt(fmm)+'</div><div class="l">Objects with field changes &#8594;</div></div>';
  if(D.metadata&&D.metadata.mode==='validate-live'){{
    var syncFailed=(D.executive_summary&&D.executive_summary.total_sync_failed)||0;
    h+='<div class="card" style="cursor:pointer" onclick="jumpToSyncs()" title="Failed project or inventory source syncs on the live target"><div class="v'+(syncFailed===0?' ok':' bad')+'">'+fmt(syncFailed)+'</div><div class="l">Failed syncs &#8594;</div></div>';
  }}
  h+='<div class="card" style="cursor:pointer" onclick="jumpToUnexplained()" title="Missing objects with no DB explanation"><div class="v'+(unex===0?' ok':' bad')+'">'+fmt(unex)+'</div><div class="l">Unexplained &#8594;</div></div>';
  h+='</div>';

  // Migration status cards — completed/failed/skipped
  var allInv=allInvItems();
  var stC=0,stF=0,stS=0,stP=0;
  allInv.forEach(function(e){{if(e.st==='c')stC++;else if(e.st==='f')stF++;else if(e.st==='s')stS++;else stP++}});
  h+='<h3>Migration Status</h3>';
  h+='<div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr));">';
  h+='<div class="card" style="cursor:pointer" onclick="jumpToAllObjects(\\'c\\')"><div class="v ok">'+fmt(stC)+'</div><div class="l">Completed &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="jumpToAllObjects(\\'f\\')"><div class="v'+(stF>0?' bad':' skip')+'">'+fmt(stF)+'</div><div class="l">Failed &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="jumpToAllObjects(\\'s\\')"><div class="v skip">'+fmt(stS)+'</div><div class="l">Skipped &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="jumpToAllObjects(\\'p\\')"><div class="v skip">'+fmt(stP)+'</div><div class="l">Pending &#8594;</div></div>';
  h+='</div>';

  // Org health summary — clickable
  h+='<h3>Organization Health ('+nOrgs+' orgs)</h3>';
  h+='<div class="cards" style="grid-template-columns:repeat(3,1fr);">';
  h+='<div class="card" style="cursor:pointer" onclick="orgDrill=null;orgObjType=null;orgSearch=\\'\\';orgPage=1;orgFilter=\\'green\\';switchTab(\\'orgs\\')"><div class="v ok">'+oG+'</div><div class="l">GREEN &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="orgDrill=null;orgObjType=null;orgSearch=\\'\\';orgPage=1;orgFilter=\\'amber\\';switchTab(\\'orgs\\')"><div class="v warn">'+oA+'</div><div class="l">AMBER &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="orgDrill=null;orgObjType=null;orgSearch=\\'\\';orgPage=1;orgFilter=\\'red\\';switchTab(\\'orgs\\')"><div class="v bad">'+oR+'</div><div class="l">RED &#8594;</div></div>';
  h+='</div>';

  // Per-type progress bars — clickable to drill into type
  h+='<h3>By Resource Type <span style="font-size:.78rem;color:var(--fg2);font-weight:400">&mdash; click to browse objects</span></h3>';
  var sorted=pt.slice().sort(function(a,b){{return pct(a.t2_existence.matched,a.t1_counts.source)-pct(b.t2_existence.matched,b.t1_counts.source)}});
  sorted.forEach(function(t){{
    var s=t.t1_counts.source,m=t.t2_existence.matched,mi=t.t2_existence.missing_on_target,fm=t.t3_field_parity.mismatching;
    var typeGaps=(t.t1_counts.explained_failures||0)+(t.t1_counts.explained_skips||0)+(t.t1_counts.unexplained||0);
    var pm=pctMatched(m,s,typeGaps),pmi=pct(mi,s);
    h+='<div class="pbar-row" style="cursor:pointer" onclick="allObjView=null;typeDrill=\\''+esc(t.resource_type)+'\\';objSt=\\'issues\\';objPage=1;switchTab(\\'types\\')">';
    h+='<div class="pbar-name" title="'+esc(t.resource_type)+'">'+esc(t.display_name||t.resource_type)+'</div>';
    h+='<div class="pbar-track">';
    // Existence only (matched + missing). Field mismatches are a subset of matched —
    // do not stack them as a third % segment (that overflowed past 100%).
    h+='<div class="pbar-seg" style="width:'+pm+'%;background:var(--pass)"></div>';
    if(mi>0)h+='<div class="pbar-seg" style="width:'+Math.max(pmi,1)+'%;background:var(--fail)"></div>';
    h+='</div>';
    h+='<div class="pbar-nums">'+fmt(m)+' / '+fmt(s)+' ('+pm+'%)';
    if(fm>0)h+=' <span style="color:var(--warn)" title="Objects with field changes">'+fmt(fm)+' fieldΔ</span>';
    h+='</div>';
    h+='</div>';
  }});

  document.getElementById('dashboardContent').innerHTML=h;
}}

/* ── Tab 2: Org Health ── */
var orgPage=1,orgFilter='all',orgSearch='',orgSort='health_size',orgDrill=null,orgDrillSection=null,orgObjType=null;
function renderOrgs(){{
  if(orgDrill){{renderOrgDrill(orgDrill);return}}
  var po=D.per_org||{{}};
  var okeys=Object.keys(po);
  var orgs=okeys.map(function(k){{return po[k]}});

  // Sort: default red→amber→green then total desc (configurable)
  orgs.sort(function(a,b){{
    if(orgSort==='name')return String(a.org_name||'').localeCompare(String(b.org_name||''));
    if(orgSort==='missing')return (b.missing||0)-(a.missing||0)||String(a.org_name||'').localeCompare(String(b.org_name||''));
    if(orgSort==='unexplained')return (b.unexplained||0)-(a.unexplained||0)||String(a.org_name||'').localeCompare(String(b.org_name||''));
    if(orgSort==='changed')return (b.field_mismatches||0)-(a.field_mismatches||0)||String(a.org_name||'').localeCompare(String(b.org_name||''));
    if(orgSort==='total')return (b.total_objects||0)-(a.total_objects||0)||String(a.org_name||'').localeCompare(String(b.org_name||''));
    var ha=a.health==='red'?0:a.health==='amber'?1:2, hb=b.health==='red'?0:b.health==='amber'?1:2;
    return (ha!==hb)?(ha-hb):((b.total_objects||0)-(a.total_objects||0));
  }});

  // Filter
  var pool=orgs.filter(function(o){{
    if(orgFilter!=='all'&&o.health!==orgFilter)return false;
    return true;
  }});
  var filtered=pool.filter(function(o){{
    if(orgSearch&&o.org_name!==orgSearch)return false;
    return true;
  }});

  var h='<h2>Organization Health &mdash; '+okeys.length+' Organizations</h2>';
  if({orgs_explained_failures}>0){{
    h+='<div class="callout callout-fail"><strong>Import failures:</strong> '+fmt({orgs_explained_failures})+' object(s) failed import. These are explained by the migration DB but still mark organizations as RED.</div>';
  }}

  // Dot grid
  h+='<div class="dot-legend"><span class="lg">Green ('+{orgs_green}+')</span><span class="la">Amber ('+{orgs_amber}+')</span><span class="lr">Red ('+{orgs_red}+')</span></div>';
  h+='<div class="dot-grid">';
  orgs.forEach(function(o,i){{
    var c=o.health==='red'?'r':o.health==='amber'?'a':'g';
    h+='<div class="dot '+c+'" title="'+esc(o.org_name)+' ('+o.health+')" onclick="drillOrg('+i+')"></div>';
  }});
  h+='</div>';

  // Filter bar
  h+='<div class="filter-bar">';
  h+=orgFilterHtml('flt-org-health',orgSearch,'orgSearch','orgPage=1;renderOrgs()');
  h+='<select onchange="orgFilter=this.value;orgPage=1;renderOrgs()">';
  h+='<option value="all"'+(orgFilter==='all'?' selected':'')+'>All</option>';
  h+='<option value="red"'+(orgFilter==='red'?' selected':'')+'>Red only</option>';
  h+='<option value="amber"'+(orgFilter==='amber'?' selected':'')+'>Amber only</option>';
  h+='<option value="green"'+(orgFilter==='green'?' selected':'')+'>Green only</option>';
  h+='</select>';
  h+='<select onchange="orgSort=this.value;orgPage=1;renderOrgs()">';
  h+='<option value="health_size"'+(orgSort==='health_size'?' selected':'')+'>Sort: health, size</option>';
  h+='<option value="name"'+(orgSort==='name'?' selected':'')+'>Sort: name</option>';
  h+='<option value="total"'+(orgSort==='total'?' selected':'')+'>Sort: total objects</option>';
  h+='<option value="missing"'+(orgSort==='missing'?' selected':'')+'>Sort: missing</option>';
  h+='<option value="unexplained"'+(orgSort==='unexplained'?' selected':'')+'>Sort: unexplained</option>';
  h+='<option value="changed"'+(orgSort==='changed'?' selected':'')+'>Sort: changed</option>';
  h+='</select>';
  if(orgFilter!=='all'||orgSearch)h+='<button onclick="orgFilter=\\'all\\';orgSearch=\\'\\';orgPage=1;renderOrgs()">Clear</button>';
  h+='</div>';

  // Paginated table
  var PER=25,total=filtered.length,pages=Math.ceil(total/PER)||1;
  orgPage=clampPage(orgPage,pages);
  var start=(orgPage-1)*PER,slice=filtered.slice(start,start+PER);

  h+=showingLine(filtered.length,pool.length,'organizations',orgs.length);
  h+='<table><thead><tr><th>Organization</th><th style="width:200px">Progress</th><th class="num">Objects</th><th class="num">Missing</th><th class="num">Failed</th><th class="num">Changed</th><th class="num">Unexplained</th><th>Health</th></tr></thead><tbody>';
  if(!slice.length){{
    h+='<tr><td colspan="8" class="empty-msg">No organizations match your filter.</td></tr>';
  }}
  slice.forEach(function(o){{
    var pm=pct(o.matched,o.total_objects);
    var fails=o.explained_failures||0;
    h+='<tr class="clickable" onclick="drillOrgByName(\\''+esc(o.org_name).replace(/'/g,"\\\\'")+'\\')">';
    h+='<td>'+objD(o.org_name,'',o.source_id,o.target_id)+'</td>';
    h+='<td><div class="pbar-track"><div class="pbar-seg" style="width:'+pm+'%;background:var(--pass)"></div>';
    if(o.missing>0)h+='<div class="pbar-seg" style="width:'+Math.max(pct(o.missing,o.total_objects),1)+'%;background:var(--fail)"></div>';
    h+='</div></td>';
    h+='<td class="num">'+fmt(o.total_objects)+'</td>';
    h+='<td class="num">'+fmt(o.missing)+'</td>';
    h+='<td class="num"'+(fails>0?' style="color:var(--fail);font-weight:700"':'')+'>'+fmt(fails)+'</td>';
    h+='<td class="num">'+fmt(o.field_mismatches)+'</td>';
    h+='<td class="num"'+(o.unexplained>0?' style="color:var(--fail);font-weight:700"':'')+'>'+fmt(o.unexplained)+'</td>';
    h+='<td><span class="health '+hcls(o.health)+'">'+hlbl(o.health)+'</span></td>';
    h+='</tr>';
  }});
  h+='</tbody></table>';

  if(pages>1){{
    h+='<div class="pager">';
    h+='<button onclick="orgPage--;renderOrgs()"'+(orgPage<=1?' disabled':'')+'>&#9664; Prev</button>';
    h+='<span class="pg-info">Page '+orgPage+' of '+pages+' ('+total+' orgs)</span>';
    h+='<button onclick="orgPage++;renderOrgs()"'+(orgPage>=pages?' disabled':'')+'>Next &#9654;</button>';
    h+='</div>';
  }}

  document.getElementById('orgsContent').innerHTML=h;
  restoreKeptFocus();
}}

function drillOrg(idx){{
  var okeys=Object.keys(D.per_org||{{}});
  var orgs=okeys.map(function(k){{return D.per_org[k]}});
  orgs.sort(function(a,b){{var ha=a.health==='red'?0:a.health==='amber'?1:2;var hb=b.health==='red'?0:b.health==='amber'?1:2;if(ha!==hb)return ha-hb;return b.total_objects-a.total_objects}});
  if(idx>=0&&orgs.length>idx){{orgDrill=orgs[idx].org_name;switchTab('orgs')}}
}}
function drillOrgByName(name){{orgDrill=name;renderOrgs()}}

function renderOrgDrill(orgName){{
  if(orgObjType){{renderOrgObj(orgName,orgObjType);return}}
  var o=(D.per_org||{{}})[orgName];
  if(!o){{orgDrill=null;renderOrgs();return}}
  var fails=o.explained_failures||0;
  var skips=o.explained_skips||0;
  var h='<div class="drill">';
  h+='<button class="back-btn" onclick="orgDrill=null;renderOrgs()">&#9664; Back to all orgs</button>';
  h+='<h2>'+esc(o.org_name)+' '+ids(o.source_id,o.target_id)+' <span class="health '+hcls(o.health)+'">'+hlbl(o.health)+'</span></h2>';
  if(fails>0){{
    h+='<div class="callout callout-fail"><strong>Import failures:</strong> '+fmt(fails)+' object(s) failed import in this organization (explained by migration DB, still treated as failures).</div>';
  }}

  // Summary cards — clickable where data exists
  h+='<div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(120px,1fr));">';
  h+='<div class="card"><div class="v">'+fmt(o.total_objects)+'</div><div class="l">Source objects</div></div>';
  h+='<div class="card"><div class="v ok">'+fmt(o.matched)+'</div><div class="l">Matched</div></div>';
  if(o.missing>0){{
    h+='<div class="card" style="cursor:pointer" onclick="orgDrillSection=\\'missing\\';renderOrgDrill(\\''+esc(o.org_name).replace(/'/g,"\\\\'")+'\\')">';
    h+='<div class="v bad">'+fmt(o.missing)+'</div><div class="l">Missing &#8594;</div></div>';
  }}else{{
    h+='<div class="card"><div class="v ok">0</div><div class="l">Missing</div></div>';
  }}
  h+='<div class="card"><div class="v'+(fails>0?' bad':' ok')+'">'+fmt(fails)+'</div><div class="l">Failed imports</div></div>';
  if(skips>0)h+='<div class="card"><div class="v skip">'+fmt(skips)+'</div><div class="l">Skipped gaps</div></div>';
  if(o.field_mismatches>0){{
    h+='<div class="card" style="cursor:pointer" onclick="orgDrillSection=\\'fields\\';renderOrgDrill(\\''+esc(o.org_name).replace(/'/g,"\\\\'")+'\\')">';
    h+='<div class="v warn">'+fmt(o.field_mismatches)+'</div><div class="l">Changed &#8594;</div></div>';
  }}else{{
    h+='<div class="card"><div class="v ok">0</div><div class="l">Changed</div></div>';
  }}
  h+='<div class="card"><div class="v'+(o.unexplained===0?' ok':' bad')+'">'+fmt(o.unexplained)+'</div><div class="l">Unexplained</div></div>';
  h+='</div>';

  // Accounting callout
  h+='<div class="callout callout-info">'+fmt(o.total_objects)+' source = '+fmt(o.matched)+' matched + '+fmt(fails)+' failed + '+fmt(skips)+' skipped + '+fmt(o.unexplained)+' unexplained';
  if(o.field_mismatches>0)h+=' &middot; '+fmt(o.field_mismatches)+' with field changes';
  h+='</div>';

  // Per-type table — click any row to browse objects
  if(o.per_type&&o.per_type.length){{
    h+='<h3>By Resource Type <span style="font-size:.78rem;color:var(--fg2);font-weight:400">&mdash; click a type to browse objects</span></h3>';
    h+='<table><thead><tr><th>Type</th><th style="width:200px">Progress</th><th class="num">Source</th><th class="num">Matched</th><th class="num">Missing</th><th class="num">Changed</th><th></th></tr></thead><tbody>';
    o.per_type.forEach(function(t){{
      var total=t.source||1;
      var oSafe=esc(o.org_name).replace(/'/g,"\\\\'");
      h+='<tr class="clickable" onclick="orgObjType=\\''+esc(t.resource_type)+'\\';objSt=\\'issues\\';objOrg=\\'\\';objSearch=\\'\\';objPage=1;renderOrgDrill(\\''+oSafe+'\\')">';
      h+='<td><strong>'+esc(t.resource_type)+'</strong> <span style="font-size:.72rem;color:var(--fg2)">&#8594;</span></td>';
      h+='<td><div class="pbar-track">';
      h+='<div class="pbar-seg" style="width:'+pct(t.matched,total)+'%;background:var(--pass)"></div>';
      if(t.missing>0)h+='<div class="pbar-seg" style="width:'+Math.max(pct(t.missing,total),1)+'%;background:var(--fail)"></div>';
      h+='</div></td>';
      h+='<td class="num">'+fmt(t.source)+'</td>';
      h+='<td class="num" style="color:var(--pass)">'+fmt(t.matched)+'</td>';
      h+='<td class="num"'+(t.missing>0?' style="color:var(--fail);font-weight:700"':'')+'>'+fmt(t.missing)+'</td>';
      h+='<td class="num"'+(t.field_mismatches>0?' style="color:var(--warn);font-weight:700"':'')+'>'+fmt(t.field_mismatches)+'</td>';
      h+='<td style="font-size:.78rem">';
      if(t.missing>0)h+='<a href="#" style="color:var(--fail);margin-right:6px" onclick="event.stopPropagation();event.preventDefault();jumpToMissing(\\''+esc(t.resource_type)+'\\',\\''+oSafe+'\\')" title="View in Missing tab">missing</a>';
      if(t.field_mismatches>0)h+='<a href="#" style="color:var(--warn)" onclick="event.stopPropagation();event.preventDefault();jumpToFields(\\''+esc(t.resource_type)+'\\',\\''+oSafe+'\\')" title="View in Fields tab">changes</a>';
      h+='</td>';
      h+='</tr>';
    }});
    h+='</tbody></table>';
  }}

  // Missing details section (expanded if drilled from card click)
  var md=o.missing_details||[];
  if(md.length){{
    var misOpen=orgDrillSection==='missing';
    h+='<div class="grp-hd'+(misOpen?' open':'')+'" onclick="this.classList.toggle(\\'open\\');this.nextElementSibling.classList.toggle(\\'open\\')">';
    h+='<span class="arrow">&#9654;</span> Missing Objects ('+md.length+')';
    h+='</div>';
    h+='<div class="grp-bd'+(misOpen?' open':'')+'">';
    h+='<table><thead><tr><th>Type</th><th>Object</th><th>Explanation</th></tr></thead><tbody>';
    md.slice(0,100).forEach(function(m){{
      h+='<tr><td><code>'+esc(m.parent_type||'')+'</code></td>';
      h+='<td>'+objD(m.name,m.organization,m.source_id,m.target_id)+'</td>';
      h+='<td style="font-size:.82rem;">'+esc(m.explanation)+'</td></tr>';
    }});
    if(md.length>100){{
      h+='<tr><td colspan="3" class="empty-msg">Showing 100 of '+md.length+'. ';
      h+='<a href="#" onclick="event.preventDefault();jumpToMissing(\\'all\\',\\''+esc(o.org_name).replace(/'/g,"\\\\'")+'\\')" style="color:var(--accent)">View all in Missing tab &rarr;</a></td></tr>';
    }}
    h+='</tbody></table></div>';
  }}

  // Field findings section
  var ff=o.field_findings||[];
  if(ff.length){{
    var fldOpen=orgDrillSection==='fields';
    h+='<div class="grp-hd'+(fldOpen?' open':'')+'" onclick="this.classList.toggle(\\'open\\');this.nextElementSibling.classList.toggle(\\'open\\')">';
    h+='<span class="arrow">&#9654;</span> Field Changes ('+ff.length+')';
    h+='</div>';
    h+='<div class="grp-bd'+(fldOpen?' open':'')+'">';
    ff.slice(0,20).forEach(function(f){{
      h+='<div class="finding"><div class="finding-hd"><span>'+objD(f.name,f.organization,f.source_id,f.target_id)+'</span><span>'+esc(f.tier)+'</span></div>';
      h+='<div class="finding-bd"><table>';
      h+='<tr><td>Field</td><td><code>'+esc(f.field)+'</code></td></tr>';
      h+='<tr><td>Source</td><td class="src-val">'+esc(f.source_value)+'</td></tr>';
      h+='<tr><td>Target</td><td class="tgt-val">'+esc(f.target_value)+'</td></tr>';
      h+='</table></div></div>';
    }});
    if(ff.length>20){{
      h+='<div class="callout callout-info">Showing 20 of '+ff.length+'. ';
      h+='<a href="#" onclick="event.preventDefault();jumpToFields(\\'all\\',\\''+esc(o.org_name).replace(/'/g,"\\\\'")+'\\')" style="color:var(--accent)">View all in Fields tab &rarr;</a></div>';
    }}
    h+='</div>';
  }}

  h+='</div>';
  orgDrillSection=null;
  document.getElementById('orgsContent').innerHTML=h;
}}

/* ── Org + Type Object Browser ── */
function renderOrgObj(orgName,rt){{
  var o=(D.per_org||{{}})[orgName];
  if(!o){{orgObjType=null;orgDrill=null;renderOrgs();return}}
  var items=invForTypeOrg(rt,o.org_name);
  var pool=items.filter(function(e){{
    if(!matchesObjFilter(e))return false;
    return true;
  }});
  var filtered=sortProblemsFirst(pool.filter(function(e){{
    if(objSearch&&e.n.toLowerCase().indexOf(objSearch.toLowerCase())<0)return false;
    return true;
  }}));
  var cC=0,cF=0,cS=0,cP=0,cFc=0;
  items.forEach(function(e){{
    if(e.st==='c')cC++;else if(e.st==='f')cF++;else if(e.st==='s')cS++;else cP++;
    if(e.fc)cFc++;
  }});

  var oSafe=esc(o.org_name).replace(/'/g,"\\\\'");
  var cIssues=countIssueObjects(items);
  var h='<div class="drill">';
  h+='<button class="back-btn" onclick="objSt=\\'issues\\';objSearch=\\'\\';objPage=1;orgObjType=null;renderOrgDrill(\\''+oSafe+'\\')">&#9664; Back to '+esc(o.org_name)+'</button>';
  h+='<h2>'+esc(o.org_name)+' &rarr; '+esc(rt)+'</h2>';

  h+='<div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr));">';
  h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'issues\\';objPage=1;renderOrgObj(\\''+oSafe+'\\',\\''+esc(rt)+'\\')">';
  h+='<div class="v'+(cIssues>0?' bad':' ok')+'">'+fmt(cIssues)+'</div><div class="l">Issues &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'all\\';objPage=1;renderOrgObj(\\''+oSafe+'\\',\\''+esc(rt)+'\\')">';
  h+='<div class="v">'+fmt(items.length)+'</div><div class="l">Total objects</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'c\\';objPage=1;renderOrgObj(\\''+oSafe+'\\',\\''+esc(rt)+'\\')">';
  h+='<div class="v ok">'+fmt(cC)+'</div><div class="l">Completed objects</div></div>';
  if(cFc){{h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'fc\\';objPage=1;renderOrgObj(\\''+oSafe+'\\',\\''+esc(rt)+'\\')">';
  h+='<div class="v warn">'+fmt(cFc)+'</div><div class="l">Fields changed &#8594;</div></div>';}}
  if(cF){{h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'f\\';objPage=1;renderOrgObj(\\''+oSafe+'\\',\\''+esc(rt)+'\\')">';
  h+='<div class="v bad">'+fmt(cF)+'</div><div class="l">Failed objects</div></div>';}}
  if(cS){{h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'s\\';objPage=1;renderOrgObj(\\''+oSafe+'\\',\\''+esc(rt)+'\\')">';
  h+='<div class="v skip">'+fmt(cS)+'</div><div class="l">Skipped objects</div></div>';}}
  if(cP){{h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'p\\';objPage=1;renderOrgObj(\\''+oSafe+'\\',\\''+esc(rt)+'\\')">';
  h+='<div class="v skip">'+fmt(cP)+'</div><div class="l">Pending objects</div></div>';}}
  h+='</div>';

  h+=renderObjectTable(items,pool,filtered,'orgsContent','&#9664; Back','renderOrgObj(\\''+oSafe+'\\',\\''+esc(rt)+'\\')',rt,'flt-orgobj');
  h+='</div>';
  document.getElementById('orgsContent').innerHTML=h;
  restoreKeptFocus();
}}

/* ── Tab 3: Resource Types ── */
var typeDrill=null,allObjView=null,allObjType='all';
function allInvItems(){{
  var all=[];
  Object.keys(INV).forEach(function(rt){{
    (INV[rt]||[]).forEach(function(e){{all.push({{_rt:rt,n:e.n,o:e.o,s:e.s,t:e.t,st:e.st,e:e.e,fc:e.fc,_ref:e}})}})
  }});
  return all;
}}
function renderAllObjects(){{
  var items=allInvItems();
  var cC=0,cF=0,cS=0,cP=0,cFc=0;
  items.forEach(function(e){{
    if(e.st==='c')cC++;else if(e.st==='f')cF++;else if(e.st==='s')cS++;else cP++;
    if(e.fc)cFc++;
  }});
  var pool=sortProblemsFirst(items.filter(function(e){{
    if(!matchesObjFilter(e))return false;
    if(allObjType!=='all'&&e._rt!==allObjType)return false;
    if(objOrg&&e.o!==objOrg)return false;
    return true;
  }}));
  var filtered=sortProblemsFirst(pool.filter(function(e){{
    if(objSearch&&e.n.toLowerCase().indexOf(objSearch.toLowerCase())<0)return false;
    return true;
  }}));
  var cIssues=countIssueObjects(items);
  var h='<div class="drill">';
  h+='<button class="back-btn" onclick="objSt=\\'issues\\';objOrg=\\'\\';objSearch=\\'\\';objPage=1;allObjType=\\'all\\';allObjView=null;typeDrill=null;renderTypes()">&#9664; Back to types</button>';
  h+='<h2>All Objects &mdash; '+fmt(items.length)+' total</h2>';
  h+='<div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr));">';
  h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'issues\\';objPage=1;renderAllObjects()"><div class="v'+(cIssues>0?' bad':' ok')+'">'+fmt(cIssues)+'</div><div class="l">Issues &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'all\\';objPage=1;renderAllObjects()"><div class="v">'+fmt(items.length)+'</div><div class="l">Total objects</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'c\\';objPage=1;renderAllObjects()"><div class="v ok">'+fmt(cC)+'</div><div class="l">Completed objects</div></div>';
  if(cFc){{h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'fc\\';objPage=1;renderAllObjects()"><div class="v warn">'+fmt(cFc)+'</div><div class="l">Fields changed &#8594;</div></div>';}}
  h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'f\\';objPage=1;renderAllObjects()"><div class="v'+(cF>0?' bad':' skip')+'">'+fmt(cF)+'</div><div class="l">Failed objects</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'s\\';objPage=1;renderAllObjects()"><div class="v skip">'+fmt(cS)+'</div><div class="l">Skipped objects</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'p\\';objPage=1;renderAllObjects()"><div class="v skip">'+fmt(cP)+'</div><div class="l">Pending objects</div></div>';
  h+='</div>';

  h+='<div class="filter-bar">';
  h+='<select onchange="objSt=this.value;objPage=1;renderAllObjects()">';
  h+='<option value="issues"'+(objSt==='issues'?' selected':'')+'>Issues only</option>';
  h+='<option value="all"'+(objSt==='all'?' selected':'')+'>All</option>';
  h+='<option value="c"'+(objSt==='c'?' selected':'')+'>Completed</option>';
  h+='<option value="fc"'+(objSt==='fc'?' selected':'')+'>Fields changed</option>';
  h+='<option value="f"'+(objSt==='f'?' selected':'')+'>Failed</option>';
  h+='<option value="s"'+(objSt==='s'?' selected':'')+'>Skipped</option>';
  h+='<option value="p"'+(objSt==='p'?' selected':'')+'>Pending</option>';
  h+='</select>';
  var types=allTypes();
  h+='<select onchange="allObjType=this.value;objPage=1;renderAllObjects()">';
  h+='<option value="all"'+(allObjType==='all'?' selected':'')+'>All types</option>';
  types.forEach(function(t){{h+='<option value="'+esc(t)+'"'+(allObjType===t?' selected':'')+'>'+esc(t)+'</option>'}});
  h+='</select>';
  h+=orgFilterHtml('flt-org-allobj',objOrg,'objOrg','objPage=1;renderAllObjects()');
  h+=searchFilterHtml('flt-name-allobj',objSearch,'Search by name...','objSearch=this.value;objPage=1;renderAllObjects()','objSearch=\\'\\';objPage=1;renderAllObjects()');
  if(objSt!=='issues'||allObjType!=='all'||objOrg||objSearch)h+='<button onclick="objSt=\\'issues\\';allObjType=\\'all\\';objOrg=\\'\\';objSearch=\\'\\';objPage=1;renderAllObjects()">Clear</button>';
  h+='</div>';

  var PER=100,total=filtered.length,pages=Math.ceil(total/PER)||1;
  objPage=clampPage(objPage,pages);
  var start=(objPage-1)*PER,slice=filtered.slice(start,start+PER);
  h+=showingLine(filtered.length,pool.length,'objects',items.length);
  h+='<table class="obj-tbl"><thead><tr><th>Object Name</th><th>Type</th><th>Organization</th><th class="num">Source ID</th><th class="num">Target ID</th><th>Status</th><th>Error / Notes</th></tr></thead><tbody>';
  if(!slice.length)h+='<tr><td colspan="7" class="empty-msg">No objects match your filters.</td></tr>';
  slice.forEach(function(e){{
    var invArr=INV[e._rt]||[];var idx=invArr.indexOf(e._ref);if(idx<0)for(var ii=0;invArr.length>ii;ii++){{if(invArr[ii].s===e.s&&invArr[ii].n===e.n){{idx=ii;break}}}}
    var oc=idx>=0?(' onclick="toggleObjectDetail(\\''+esc(e._rt)+'\\','+idx+',this)"'):' ';
    var rowCls='row-'+e.st+(e.fc?' row-fc':'');
    h+='<tr class="'+rowCls+'"'+oc+' title="Click for details">';
    h+='<td><strong>'+esc(e.n)+'</strong></td>';
    h+='<td style="font-size:.78rem">'+esc(e._rt)+'</td>';
    h+='<td>'+esc(e.o||'—')+'</td>';
    h+='<td class="num">'+(e.s!=null?e.s:'—')+'</td>';
    h+='<td class="num">'+(e.t!=null?e.t:'—')+'</td>';
    h+='<td>'+stBadge(e.st,e.fc)+'</td>';
    var note=e.e||(e.fc?'Field differences vs source':'');
    h+='<td class="'+(e.e?'err':(e.fc?'note-fc':''))+'" title="'+esc(note)+'">'+esc(note||'—')+'</td>';
    h+='</tr>';
  }});
  h+='</tbody></table>';
  if(pages>1){{
    h+='<div class="pager">';
    h+='<button onclick="objPage--;renderAllObjects()"'+(objPage<=1?' disabled':'')+'>&#9664; Prev</button>';
    h+='<span class="pg-info">Page '+objPage+' of '+pages+' ('+total+' objects)</span>';
    h+='<button onclick="objPage++;renderAllObjects()"'+(objPage>=pages?' disabled':'')+'>Next &#9654;</button>';
    h+='</div>';
  }}
  h+='</div>';
  document.getElementById('typesContent').innerHTML=h;
  restoreKeptFocus();
}}
/* ── Tab 3: Resource Types ── */
var typesBucketFilter='all';
function renderTypes(){{
  if(allObjView){{renderAllObjects();return}}
  if(typeDrill){{renderTypeDrill(typeDrill);return}}
  var pt=D.per_type;

  var allInv=allInvItems();
  var tC=0,tF=0,tS=0,tP=0;
  pt.forEach(function(t){{
    var bucket=typeMigrationBucket(t);
    if(bucket==='f')tF++;
    else if(bucket==='s')tS++;
    else if(bucket==='p')tP++;
    else tC++;
  }});

  var h='<h2>Resource Types &mdash; T1 <span style="font-size:.78rem;color:var(--fg2);font-weight:400">('+pt.length+' types)</span></h2>';
  h+='<div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr));margin-bottom:.8rem">';
  h+='<div class="card" style="cursor:pointer" onclick="allObjView=true;objSt=\\'issues\\';objPage=1;renderTypes()"><div class="v">'+fmt(allInv.length)+'</div><div class="l">All Objects &#8594;</div></div>';
  h+='<div class="card"><div class="v ok">'+fmt(tC)+'</div><div class="l">Successful resource types</div></div>';
  h+='<div class="card"><div class="v'+(tF>0?' bad':' skip')+'">'+fmt(tF)+'</div><div class="l">Resource Types with Failures</div></div>';
  h+='<div class="card"><div class="v skip">'+fmt(tS)+'</div><div class="l">Skipped resource types</div></div>';
  h+='<div class="card"><div class="v skip">'+fmt(tP)+'</div><div class="l">Pending resource types</div></div>';
  h+='</div>';
  h+='<div class="callout callout-info">Status cards above summarize resource types. Use <strong>All Objects</strong> to browse object-level records (loads on click), or click a type below. Types with gaps/changes appear first.</div>';
  if(typesBucketFilter!=='all'){{
    var bucketLabel=typesBucketFilter==='f'?'failed':typesBucketFilter==='s'?'skipped':typesBucketFilter==='p'?'pending':'complete';
    h+='<div class="callout callout-info">Showing <strong>'+esc(bucketLabel)+'</strong> resource types only. <a href="#" onclick="event.preventDefault();typesBucketFilter=\\'all\\';renderTypes()">Show all types</a></div>';
  }}
  h+='<div class="type-legend">';
  h+='<span><i class="ok"></i>Matched / field OK</span>';
  h+='<span><i class="bad"></i>Missing on target</span>';
  h+='<span><i class="warn"></i>Objects with field changes</span>';
  h+='</div>';

  var sorted=pt.slice().sort(function(a,b){{
    function score(t){{
      var e=t.t2_existence||{{}},c=t.t1_counts||{{}},fp=t.t3_field_parity||{{}};
      return (e.missing_on_target||0)*1000+(c.unexplained||0)*100+(fp.mismatching||0)*10+(c.explained_failures||0);
    }}
    var d=score(b)-score(a);
    if(d)return d;
    return String(a.resource_type).localeCompare(String(b.resource_type));
  }});
  if(typesBucketFilter!=='all'){{
    sorted=sorted.filter(function(t){{return typeMigrationBucket(t)===typesBucketFilter}});
  }}

  sorted.forEach(function(t){{
    var c=t.t1_counts,e=t.t2_existence,fp=t.t3_field_parity;
    var expl=c.explained_failures+c.explained_skips;
    var invCount=(INV[t.resource_type]||[]).length;
    var src=c.source||0;
    var matched=e.matched||0;
    var missing=e.missing_on_target||0;
    // Scale bars via flex shares = object counts (exact proportions per type)
    var fieldOk=fp.matching||0;
    var fieldBad=fp.mismatching||0;
    var fieldCompared=fp.compared||(fieldOk+fieldBad);

    h+='<div class="type-card" onclick="allObjView=null;typeDrill=\\''+esc(t.resource_type)+'\\';objSt=\\'issues\\';objPage=1;renderTypes()">';
    h+='<div class="type-card-head">';
    h+='<strong>'+esc(t.display_name||t.resource_type)+'</strong>';
    h+='<span class="type-card-meta">'+fmt(src)+' source &middot; '+fmt(c.target)+' target &#8594;</span>';
    h+='</div>';

    // Count / existence bar (T1+T2)
    h+='<div class="type-bar-row">';
    h+='<div class="type-bar-label">Counts</div>';
    h+='<div class="type-bar-track">';
    if(src>0||matched>0||missing>0){{
      if(matched>0)h+='<div class="type-bar-seg ok" style="flex:'+matched+'" title="Matched: '+fmt(matched)+'"></div>';
      if(missing>0)h+='<div class="type-bar-seg bad" style="flex:'+missing+'" title="Missing: '+fmt(missing)+'"></div>';
      // Pad remainder when matched+missing does not cover source count
      var covered=matched+missing;
      if(src>covered)h+='<div class="type-bar-seg" style="flex:'+(src-covered)+';background:#ced4da" title="Unaccounted"></div>';
    }}
    h+='</div>';
    h+='<div class="type-bar-nums">'+fmt(matched)+' / '+fmt(src);
    if(missing>0)h+=' <span style="color:var(--fail)">&minus;'+fmt(missing)+'</span>';
    h+='</div></div>';

    // Field parity by object (T3) — one object with N field diffs counts once
    h+='<div class="type-bar-row">';
    h+='<div class="type-bar-label">Fields</div>';
    h+='<div class="type-bar-track">';
    if(fieldCompared>0){{
      if(fieldOk>0)h+='<div class="type-bar-seg ok" style="flex:'+fieldOk+'" title="Objects with matching fields: '+fmt(fieldOk)+'"></div>';
      if(fieldBad>0)h+='<div class="type-bar-seg warn" style="flex:'+fieldBad+'" title="Objects with field changes: '+fmt(fieldBad)+'"></div>';
    }}
    h+='</div>';
    if(fieldCompared>0){{
      h+='<div class="type-bar-nums">'+fmt(fieldOk)+' matching';
      if(fieldBad>0)h+=' <span style="color:var(--warn)">'+fmt(fieldBad)+' changed</span>';
      h+='</div>';
    }}else{{
      h+='<div class="type-bar-nums" style="font-style:italic">not compared</div>';
    }}
    h+='</div>';

    h+='<div class="type-card-stats">';
    if(e.extra_on_target){{
      if(t.resource_type==='hosts'){{
        h+='<span title="Host extras are counted here but not listed on Extra or Hosts tabs (Hosts shows inventory parity / sample)">Extra on target: <strong>'+fmt(e.extra_on_target)+'</strong> <span class="ids">(count only)</span></span>';
      }}else{{
        h+='<span>Extra on target: <a href="#" onclick="event.preventDefault();event.stopPropagation();jumpToExtra(\\''+esc(t.resource_type)+'\\')" style="color:inherit"><strong>'+fmt(e.extra_on_target)+'</strong></a></span>';
      }}
    }}
    h+='<span>Explained: '+fmt(expl)+'</span>';
    if(c.unexplained>0)h+='<span><strong style="color:var(--fail)">'+c.unexplained+' Unexplained</strong></span>';
    if(invCount)h+='<span>'+fmt(invCount)+' object records</span>';
    h+='</div>';
    h+='</div>';
  }});

  document.getElementById('typesContent').innerHTML=h;
}}

/* ── Object Browser ── */
var objPage=1,objSt='issues',objOrg='',objSearch='',objSort='issues';

/* ── Type Drill-in ── */
function renderTypeDrill(rt){{
  var items=invForType(rt);
  if(items.length>0){{
    var firstOrg=items[0].o||'';
    var allSame=items.every(function(e){{return (e.o||'')===firstOrg}});
    if(allSame&&firstOrg&&objOrg&&objOrg!==firstOrg)objOrg='';
  }}
  var pool=items.filter(function(e){{
    if(!matchesObjFilter(e))return false;
    if(objOrg&&e.o!==objOrg)return false;
    return true;
  }});
  var filtered=sortProblemsFirst(pool.filter(function(e){{
    if(objSearch&&e.n.toLowerCase().indexOf(objSearch.toLowerCase())<0)return false;
    return true;
  }}));

  var cC=0,cF=0,cS=0,cP=0,cFc=0;
  items.forEach(function(e){{
    if(e.st==='c')cC++;else if(e.st==='f')cF++;else if(e.st==='s')cS++;else cP++;
    if(e.fc)cFc++;
  }});

  var cIssues=countIssueObjects(items);
  var h='<div class="drill">';
  h+='<button class="back-btn" onclick="objSt=\\'issues\\';objOrg=\\'\\';objSearch=\\'\\';objPage=1;typeDrill=null;renderTypes()">&#9664; Back to all types</button>';
  h+='<h2>'+esc(rt)+' &mdash; Object Inventory</h2>';

  h+='<div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr));">';
  h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'issues\\';objPage=1;renderTypeDrill(\\''+esc(rt)+'\\')">';
  h+='<div class="v'+(cIssues>0?' bad':' ok')+'">'+fmt(cIssues)+'</div><div class="l">Issues &#8594;</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'all\\';objPage=1;renderTypeDrill(\\''+esc(rt)+'\\')">';
  h+='<div class="v">'+fmt(items.length)+'</div><div class="l">Total objects</div></div>';
  h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'c\\';objPage=1;renderTypeDrill(\\''+esc(rt)+'\\')">';
  h+='<div class="v ok">'+fmt(cC)+'</div><div class="l">Completed objects</div></div>';
  if(cFc){{h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'fc\\';objPage=1;renderTypeDrill(\\''+esc(rt)+'\\')">';
  h+='<div class="v warn">'+fmt(cFc)+'</div><div class="l">Fields changed &#8594;</div></div>';}}
  if(cF){{h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'f\\';objPage=1;renderTypeDrill(\\''+esc(rt)+'\\')">';
  h+='<div class="v bad">'+fmt(cF)+'</div><div class="l">Failed objects</div></div>';}}
  if(cS){{h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'s\\';objPage=1;renderTypeDrill(\\''+esc(rt)+'\\')">';
  h+='<div class="v skip">'+fmt(cS)+'</div><div class="l">Skipped objects</div></div>';}}
  if(cP){{h+='<div class="card" style="cursor:pointer" onclick="objSt=\\'p\\';objPage=1;renderTypeDrill(\\''+esc(rt)+'\\')">';
  h+='<div class="v skip">'+fmt(cP)+'</div><div class="l">Pending objects</div></div>';}}
  h+='</div>';

  h+=renderObjectTable(items,pool,filtered,'typesContent','&#9664; Back','renderTypeDrill(\\''+esc(rt)+'\\')',rt,'flt-type');
  h+='</div>';
  document.getElementById('typesContent').innerHTML=h;
  restoreKeptFocus();
}}

function renderObjectTable(items,pool,filtered,targetEl,backLabel,renderSelf,rt,fltPrefix){{
  var h='';
  var showOrg=true;
  var prefix=fltPrefix||'flt-obj';
  // Detect if all objects are from same org (then hide org column)
  if(items.length>0){{
    var firstOrg=items[0].o||'';
    var allSame=items.every(function(e){{return(e.o||'')===firstOrg}});
    if(allSame&&firstOrg)showOrg=false;
  }}

  // Filter bar
  h+='<div class="filter-bar">';
  h+='<select onchange="objSt=this.value;objPage=1;'+renderSelf+'">';
  h+='<option value="issues"'+(objSt==='issues'?' selected':'')+'>Issues only</option>';
  h+='<option value="all"'+(objSt==='all'?' selected':'')+'>All</option>';
  h+='<option value="c"'+(objSt==='c'?' selected':'')+'>Completed</option>';
  h+='<option value="fc"'+(objSt==='fc'?' selected':'')+'>Fields changed</option>';
  h+='<option value="f"'+(objSt==='f'?' selected':'')+'>Failed</option>';
  h+='<option value="s"'+(objSt==='s'?' selected':'')+'>Skipped</option>';
  h+='<option value="p"'+(objSt==='p'?' selected':'')+'>Pending</option>';
  h+='</select>';
  h+='<select onchange="objSort=this.value;objPage=1;'+renderSelf+'">';
  h+='<option value="issues"'+(objSort==='issues'?' selected':'')+'>Sort: issues first</option>';
  h+='<option value="name"'+(objSort==='name'?' selected':'')+'>Sort: name</option>';
  h+='<option value="org"'+(objSort==='org'?' selected':'')+'>Sort: org</option>';
  h+='<option value="status"'+(objSort==='status'?' selected':'')+'>Sort: status</option>';
  h+='<option value="sid"'+(objSort==='sid'?' selected':'')+'>Sort: source id</option>';
  h+='<option value="tid"'+(objSort==='tid'?' selected':'')+'>Sort: target id</option>';
  h+='</select>';
  if(showOrg)h+=orgFilterHtml(prefix+'-org',objOrg,'objOrg','objPage=1;'+renderSelf);
  h+=searchFilterHtml(prefix+'-name',objSearch,'Search by name...','objSearch=this.value;objPage=1;'+renderSelf,'objSearch=\\'\\';objPage=1;'+renderSelf);
  if(objSt!=='issues'||objSort!=='issues'||objOrg||objSearch)h+='<button onclick="objSt=\\'issues\\';objSort=\\'issues\\';objOrg=\\'\\';objSearch=\\'\\';objPage=1;'+renderSelf+'">Clear</button>';
  h+='</div>';

  var sorted=filtered.slice();
  sorted.sort(function(a,b){{
    function issueRank(e){{
      return e.st==='f'?0:e.st==='p'?1:e.fc?2:e.st==='s'?3:4;
    }}
    if(objSort==='name')return String(a.n||'').localeCompare(String(b.n||''));
    if(objSort==='org')return String(a.o||'').localeCompare(String(b.o||''))||String(a.n||'').localeCompare(String(b.n||''));
    if(objSort==='status')return String(stLabel(a.st)||'').localeCompare(String(stLabel(b.st)||''))||String(a.n||'').localeCompare(String(b.n||''));
    if(objSort==='sid')return (a.s||0)-(b.s||0)||String(a.n||'').localeCompare(String(b.n||''));
    if(objSort==='tid')return (a.t||0)-(b.t||0)||String(a.n||'').localeCompare(String(b.n||''));
    var ra=issueRank(a),rb=issueRank(b);
    if(ra!==rb)return ra-rb;
    return String(a.n||'').localeCompare(String(b.n||''));
  }});

  var pg=paginateSlice(objPage,PAGE_OBJECTS,sorted.length);
  objPage=pg.page;
  var slice=sorted.slice(pg.sliceStart,pg.sliceEnd);

  h+=showingLine(sorted.length,pool.length,'objects',items.length);

  var cols=showOrg?6:5;
  h+='<table class="obj-tbl"><thead><tr>';
  h+='<th>Object Name</th>';
  if(showOrg)h+='<th>Organization</th>';
  h+='<th class="num">Source ID</th><th class="num">Target ID</th><th>Status</th><th>Error / Notes</th>';
  h+='</tr></thead><tbody>';
  if(!slice.length){{
    h+='<tr><td colspan="'+cols+'" class="empty-msg">'+(objSt==='issues'?'No issues for this view. Switch to All to browse everything.':'No objects match your filters.')+'</td></tr>';
  }}
  var allItems=rt?(INV[rt]||[]):[];
  slice.forEach(function(e){{
    var idx=allItems.indexOf(e);if(idx<0)for(var ii=0;allItems.length>ii;ii++){{if(allItems[ii].s===e.s&&allItems[ii].n===e.n){{idx=ii;break}}}}
    var oc=idx>=0?(' onclick="toggleObjectDetail(\\''+esc(rt)+'\\','+idx+',this)"'):' ';
    var rowCls='row-'+e.st+(e.fc?' row-fc':'');
    h+='<tr class="'+rowCls+'"'+oc+' title="Click for details">';
    h+='<td><strong>'+esc(e.n)+'</strong></td>';
    if(showOrg)h+='<td>'+esc(e.o||'—')+'</td>';
    h+='<td class="num">'+(e.s!=null?e.s:'—')+'</td>';
    h+='<td class="num">'+(e.t!=null?e.t:'—')+'</td>';
    h+='<td>'+stBadge(e.st,e.fc)+'</td>';
    var note=e.e||(e.fc?'Field differences vs source':'');
    h+='<td class="'+(e.e?'err':(e.fc?'note-fc':''))+'" title="'+esc(note)+'">'+esc(note||'—')+'</td>';
    h+='</tr>';
  }});
  h+='</tbody></table>';

  if(pg.pages>1){{
    h+=renderPager(pg.page,pg.pages,sorted.length,'objPage--;'+renderSelf,'objPage++;'+renderSelf,'objects');
  }}

  return h;
}}

/* ── Tab 4: Missing Objects ── */
var misPage=1,misType='all',misOrg='',misSearch='',misExplReason='',misExplClass='',misSort='severity';
var _misOrgInit=false;
function renderMissing(){{
  seedScopedOrgOnce('_misOrgInit',function(){{return misOrg}},function(v){{misOrg=v}});
  var all=[];
  D.per_type.forEach(function(t){{
    (t.t2_existence.missing_details||[]).forEach(function(md){{
      all.push({{type:t.resource_type,name:md.name,org:md.organization,sid:md.source_id,tid:md.target_id,expl:md.explanation}});
    }});
  }});

  var h='<h2>Missing Objects &mdash; T2 <span style="font-size:.78rem;color:var(--fg2);font-weight:400">('+fmt(all.length)+' total)</span></h2>';
  if(misExplClass==='unexplained'){{
    h+='<div class="callout callout-warn"><strong>Unexplained only.</strong> Objects missing on target with no failed/skipped/pending explanation in the migration DB. <a href="#" onclick="event.preventDefault();misExplClass=\\'\\';misPage=1;renderMissing()">Show all missing</a></div>';
  }}

  if(!all.length){{
    h+='<div class="callout callout-pass">No missing objects detected.</div>';
    document.getElementById('missingContent').innerHTML=h;return;
  }}

  var types=allTypes();
  // Reasons narrowed by type / org / name (not by selected reason).
  var reasonOpts=[];
  all.forEach(function(m){{
    if(misType!=='all'&&m.type!==misType)return;
    if(misOrg&&m.org!==misOrg)return;
    if(misSearch&&m.name.toLowerCase().indexOf(misSearch.toLowerCase())<0)return;
    var e=m.expl||'Unknown';
    if(reasonOpts.indexOf(e)<0)reasonOpts.push(e);
  }});
  reasonOpts.sort(function(a,b){{return a.localeCompare(b)}});
  if(misExplReason&&reasonOpts.indexOf(misExplReason)<0)misExplReason='';

  h+='<div class="filter-bar">';
  h+='<select onchange="misType=this.value;misPage=1;renderMissing()">';
  h+='<option value="all"'+(misType==='all'?' selected':'')+'>All types</option>';
  types.forEach(function(t){{h+='<option value="'+esc(t)+'"'+(misType===t?' selected':'')+'>'+esc(t)+'</option>'}});
  h+='</select>';
  h+=reasonFilterHtml('flt-reason-missing',misExplReason,'misExplReason','misPage=1;renderMissing()',reasonOpts);
  h+=orgFilterHtml('flt-org-missing',misOrg,'misOrg','misPage=1;renderMissing()');
  h+='<select onchange="misSort=this.value;misPage=1;renderMissing()">';
  h+='<option value="severity"'+(misSort==='severity'?' selected':'')+'>Sort: severity</option>';
  h+='<option value="name"'+(misSort==='name'?' selected':'')+'>Sort: name</option>';
  h+='<option value="type"'+(misSort==='type'?' selected':'')+'>Sort: type</option>';
  h+='<option value="org"'+(misSort==='org'?' selected':'')+'>Sort: org</option>';
  h+='<option value="sid"'+(misSort==='sid'?' selected':'')+'>Sort: source id</option>';
  h+='</select>';
  h+=searchFilterHtml('flt-name-missing',misSearch,'Search object name...','misSearch=this.value;misPage=1;renderMissing()','misSearch=\\'\\';misPage=1;renderMissing()');
  if(misType!=='all'||misOrg||misSearch||misExplReason||misExplClass)h+='<button onclick="misType=\\'all\\';misOrg=\\'\\';misSearch=\\'\\';misExplReason=\\'\\';misExplClass=\\'\\';misPage=1;renderMissing()">Clear</button>';
  h+='</div>';

  var pool=all.filter(function(m){{
    if(misType!=='all'&&m.type!==misType)return false;
    if(misOrg&&m.org!==misOrg)return false;
    if(misExplClass&&explClass(m.expl)!==misExplClass)return false;
    if(misExplReason&&(m.expl||'Unknown')!==misExplReason)return false;
    return true;
  }});
  var filtered=pool.filter(function(m){{
    if(misSearch&&m.name.toLowerCase().indexOf(misSearch.toLowerCase())<0)return false;
    return true;
  }});
  // Worst explanations first: unexplained → failed → pending → skipped → live → other
  filtered.sort(function(a,b){{
    function rank(m){{var c=explClass(m.expl);return c==='unexplained'?0:c==='failed'?1:c==='pending'?2:c==='skipped'?3:c==='live'?4:5;}}
    var by=sortByNameTypeOrg(a,b,misSort);
    if(by)return by;
    var byId=sortById(a,b,misSort);
    if(byId)return byId;
    if(misSort==='severity'){{var d=rank(a)-rank(b);if(d)return d;}}
    return cmpLocale(a.name,b.name);
  }});

  var pg=paginateSlice(misPage,PAGE_MISSING,filtered.length);
  misPage=pg.page;
  var slice=filtered.slice(pg.sliceStart,pg.sliceEnd);

  h+=showingLine(filtered.length,pool.length,'',all.length);
  h+='<table><thead><tr><th>Type</th><th>Object</th><th>Explanation</th></tr></thead><tbody>';
  if(!slice.length){{
    h+='<tr><td colspan="3" class="empty-msg">No missing objects match your filters.</td></tr>';
  }}
  slice.forEach(function(m){{
    var tSafe=esc(m.type).replace(/'/g,"\\\\'"), nSafe=esc(m.name).replace(/'/g,"\\\\'"), oSafe=esc(m.org||'').replace(/'/g,"\\\\'");
    h+='<tr class="clickable" onclick="jumpToTypeObject(\\''+tSafe+'\\',\\''+nSafe+'\\',\\''+oSafe+'\\')"><td><code>'+esc(m.type)+'</code></td>';
    h+='<td>'+objD(m.name,m.org,m.sid,m.tid)+'</td>';
    h+='<td style="font-size:.82rem;max-width:400px;overflow:hidden;text-overflow:ellipsis" title="'+esc(m.expl||'')+'">'+esc(m.expl)+'</td></tr>';
  }});
  h+='</tbody></table>';

  if(pg.pages>1){{
    h+=renderPager(pg.page,pg.pages,filtered.length,'misPage--;renderMissing()','misPage++;renderMissing()');
  }}

  document.getElementById('missingContent').innerHTML=h;
  restoreKeptFocus();
}}

/* ── Tab: Extra on target (live T2) ── */
var extraPage=1,extraType='all',extraOrg='',extraSearch='',extraSort='type_name';
var _extraOrgInit=false;
function renderExtra(){{
  seedScopedOrgOnce('_extraOrgInit',function(){{return extraOrg}},function(v){{extraOrg=v}});
  var all=[];
  var truncNotes=[];
  var hostExtra=0;
  D.per_type.forEach(function(t){{
    var e=t.t2_existence||{{}};
    if(t.resource_type==='hosts')hostExtra+=(e.extra_on_target||0);
    (e.extra_details||[]).forEach(function(ed){{
      all.push({{type:t.resource_type,name:ed.name,org:ed.organization||'',tid:ed.target_id,parent:ed.parent_name||''}});
    }});
    if(e.extra_truncated){{
      truncNotes.push(t.resource_type+' (+'+fmt(e.extra_truncated_count||0)+' omitted)');
    }}
  }});

  var isLive=D.metadata&&D.metadata.mode==='validate-live';
  var h='<h2>Extra on Target &mdash; T2 <span style="font-size:.78rem;color:var(--fg2);font-weight:400">('+fmt(all.length)+' listed)</span></h2>';
  h+='<div class="callout callout-info"><strong>On target, not in export:</strong> Objects present on AAP 2.6 with no identity match to an exported source object for this run. Often created or changed on the target outside the migration set.';
  if(SCOPED_ORGS&&SCOPED_ORGS.length){{
    h+=' Scoped to: <strong>'+esc(SCOPED_ORGS.join(', '))+'</strong>.';
  }}
  h+='</div>';

  if(!isLive){{
    h+='<div class="callout callout-warn"><strong>Not run:</strong> Extra-on-target details require <code>--live</code> (target catalog is not available in database-only mode).</div>';
    document.getElementById('extraContent').innerHTML=h;return;
  }}

  if(hostExtra>0){{
    h+='<div class="callout callout-info">Host extras: <strong>'+fmt(hostExtra)+'</strong> (counted in T1/T2; see Hosts tab). Hosts are omitted from this list by default.</div>';
  }}
  if(truncNotes.length){{
    h+='<div class="callout callout-warn"><strong>Truncated:</strong> '+esc(truncNotes.join('; '))+'</div>';
  }}

  if(!all.length){{
    h+='<div class="callout callout-pass">No extra (unmatched) target objects listed for this run.</div>';
    document.getElementById('extraContent').innerHTML=h;return;
  }}

  var types=allTypes().filter(function(t){{return t!=='hosts'}});
  if(extraType==='hosts')extraType='all';
  h+='<div class="filter-bar">';
  h+='<select onchange="extraType=this.value;extraPage=1;renderExtra()">';
  h+='<option value="all"'+(extraType==='all'?' selected':'')+'>All types</option>';
  types.forEach(function(t){{h+='<option value="'+esc(t)+'"'+(extraType===t?' selected':'')+'>'+esc(t)+'</option>'}});
  h+='</select>';
  h+=orgFilterHtml('flt-org-extra',extraOrg,'extraOrg','extraPage=1;renderExtra()');
  h+='<select onchange="extraSort=this.value;extraPage=1;renderExtra()">';
  h+='<option value="type_name"'+(extraSort==='type_name'?' selected':'')+'>Sort: type, name</option>';
  h+='<option value="name"'+(extraSort==='name'?' selected':'')+'>Sort: name</option>';
  h+='<option value="type"'+(extraSort==='type'?' selected':'')+'>Sort: type</option>';
  h+='<option value="org"'+(extraSort==='org'?' selected':'')+'>Sort: org</option>';
  h+='<option value="tid"'+(extraSort==='tid'?' selected':'')+'>Sort: target id</option>';
  h+='</select>';
  h+=searchFilterHtml('flt-name-extra',extraSearch,'Search object name...','extraSearch=this.value;extraPage=1;renderExtra()','extraSearch=\\'\\';extraPage=1;renderExtra()');
  if(extraType!=='all'||extraOrg||extraSearch)h+='<button onclick="extraType=\\'all\\';extraOrg=\\'\\';extraSearch=\\'\\';extraPage=1;renderExtra()">Clear</button>';
  h+='</div>';

  var pool=all.filter(function(m){{
    if(extraType!=='all'&&m.type!==extraType)return false;
    if(extraOrg&&m.org!==extraOrg)return false;
    return true;
  }});
  var filtered=pool.filter(function(m){{
    if(extraSearch&&m.name.toLowerCase().indexOf(extraSearch.toLowerCase())<0)return false;
    return true;
  }});
  filtered.sort(function(a,b){{
    var by=sortByNameTypeOrg(a,b,extraSort);
    if(by)return by;
    var byId=sortById(a,b,extraSort);
    if(byId)return byId;
    if(extraSort==='type_name'){{
      var d=cmpLocale(a.type,b.type);if(d)return d;
    }}
    return cmpLocale(a.name,b.name);
  }});

  var pg=paginateSlice(extraPage,PAGE_EXTRA,filtered.length);
  extraPage=pg.page;
  var slice=filtered.slice(pg.sliceStart,pg.sliceEnd);

  h+=showingLine(filtered.length,pool.length,'',all.length);
  h+='<table><thead><tr><th>Type</th><th>Object</th><th>Note</th></tr></thead><tbody>';
  if(!slice.length){{
    h+='<tr><td colspan="3" class="empty-msg">No extra objects match your filters.</td></tr>';
  }}
  slice.forEach(function(m){{
    h+='<tr><td><code>'+esc(m.type)+'</code></td>';
    h+='<td>'+objD(m.name,m.org||'Global / Unscoped',null,m.tid);
    if(m.parent)h+=' <span class="ids">parent:'+esc(m.parent)+'</span>';
    h+='</td>';
    h+='<td style="font-size:.82rem;color:var(--fg2)">No matching export</td></tr>';
  }});
  h+='</tbody></table>';

  if(pg.pages>1){{
    h+=renderPager(pg.page,pg.pages,filtered.length,'extraPage--;renderExtra()','extraPage++;renderExtra()');
  }}

  document.getElementById('extraContent').innerHTML=h;
  restoreKeptFocus();
}}

/* ── Tab: Syncs (live project / inventory source updates) ── */
var syncPage=1,syncType='all',syncOrg='',syncSearch='',syncSort='name',syncShow='failed';
var _syncOrgInit=false;
function syncTypeLabel(rt){{
  if(rt==='inventory_sources')return 'Inventory source';
  if(rt==='projects')return 'Project';
  return rt||'';
}}
function syncStatusStyle(st,failed){{
  if(failed)return 'color:var(--fail);font-weight:600';
  var s=(st||'').toLowerCase();
  if(s==='successful'||s==='ok')return 'color:var(--pass);font-weight:600';
  if(s==='pending'||s==='running'||s==='waiting'||s==='new'||s==='canceling')return 'color:var(--warn);font-weight:600';
  return 'color:var(--fg2)';
}}
function renderSyncs(){{
  seedScopedOrgOnce('_syncOrgInit',function(){{return syncOrg}},function(v){{syncOrg=v}});
  var all=(D.sync_entries||[]).map(function(s){{
    return {{
      name:s.name||'',
      type:s.resource_type||'',
      org:s.organization||'',
      tid:s.target_id,
      status:s.sync_status||'',
      failed:!!s.failed,
      jobId:s.last_job_id
    }};
  }});
  var isLive=D.metadata&&D.metadata.mode==='validate-live';
  var failedCount=all.filter(function(s){{return s.failed}}).length;
  var h='<h2>Syncs <span style="font-size:.78rem;color:var(--fg2);font-weight:400">('+fmt(all.length)+' projects &amp; inventory sources)</span></h2>';
  h+='<div class="callout callout-info"><strong>Project and inventory source updates:</strong> SCM/update sync status from the live target API. Use the last job ID in AAP to inspect failure details.';
  if(SCOPED_ORGS&&SCOPED_ORGS.length){{
    h+=' Scoped to: <strong>'+esc(SCOPED_ORGS.join(', '))+'</strong>.';
  }}
  h+='</div>';

  if(!isLive){{
    h+='<div class="callout callout-warn"><strong>Not run:</strong> Sync status requires <code>--live</code>.</div>';
    document.getElementById('syncsContent').innerHTML=h;return;
  }}

  if(!all.length){{
    h+='<div class="callout callout-warn">No projects or inventory sources in this run.</div>';
    document.getElementById('syncsContent').innerHTML=h;return;
  }}

  h+='<div class="filter-bar">';
  h+='<select onchange="syncShow=this.value;syncPage=1;renderSyncs()">';
  h+='<option value="failed"'+(syncShow==='failed'?' selected':'')+'>Failed only ('+fmt(failedCount)+')</option>';
  h+='<option value="all"'+(syncShow==='all'?' selected':'')+'>All syncs ('+fmt(all.length)+')</option>';
  h+='</select>';
  h+='<select onchange="syncType=this.value;syncPage=1;renderSyncs()">';
  h+='<option value="all"'+(syncType==='all'?' selected':'')+'>All types</option>';
  h+='<option value="projects"'+(syncType==='projects'?' selected':'')+'>Projects</option>';
  h+='<option value="inventory_sources"'+(syncType==='inventory_sources'?' selected':'')+'>Inventory sources</option>';
  h+='</select>';
  h+=orgFilterHtml('flt-org-sync',syncOrg,'syncOrg','syncPage=1;renderSyncs()');
  h+='<select onchange="syncSort=this.value;syncPage=1;renderSyncs()">';
  h+='<option value="name"'+(syncSort==='name'?' selected':'')+'>Sort: name</option>';
  h+='<option value="type"'+(syncSort==='type'?' selected':'')+'>Sort: type</option>';
  h+='<option value="org"'+(syncSort==='org'?' selected':'')+'>Sort: org</option>';
  h+='<option value="status"'+(syncSort==='status'?' selected':'')+'>Sort: sync status</option>';
  h+='<option value="job_id"'+(syncSort==='job_id'?' selected':'')+'>Sort: last job ID</option>';
  h+='</select>';
  h+=searchFilterHtml('flt-name-sync',syncSearch,'Search object name...','syncSearch=this.value;syncPage=1;renderSyncs()','syncSearch=\\'\\';syncPage=1;renderSyncs()');
  if(syncShow!=='failed'||syncType!=='all'||syncOrg||syncSearch)h+='<button onclick="syncShow=\\'failed\\';syncType=\\'all\\';syncOrg=\\'\\';syncSearch=\\'\\';syncPage=1;renderSyncs()">Clear</button>';
  h+='</div>';

  function syncPoolFilter(s){{
    if(syncShow==='failed'&&!s.failed)return false;
    if(syncType!=='all'&&s.type!==syncType)return false;
    if(syncOrg&&s.org!==syncOrg)return false;
    return true;
  }}
  var pool=all.filter(syncPoolFilter);
  var filtered=pool.filter(function(s){{
    if(syncSearch&&s.name.toLowerCase().indexOf(syncSearch.toLowerCase())<0)return false;
    return true;
  }});
  filtered.sort(function(a,b){{
    var by=sortByNameTypeOrg(a,b,syncSort);
    if(by)return by;
    if(syncSort==='status')return cmpLocale(a.status,b.status);
    if(syncSort==='job_id'){{
      var ai=a.jobId==null?-1:a.jobId,bi=b.jobId==null?-1:b.jobId;
      if(ai<bi)return -1;if(ai>bi)return 1;return cmpLocale(a.name,b.name);
    }}
    return cmpLocale(a.name,b.name);
  }});

  if(!filtered.length){{
    if(syncShow==='failed'&&failedCount===0){{
      h+='<div class="callout callout-pass">No failed syncs detected.</div>';
    }}else{{
      h+='<div class="callout callout-warn">No sync rows match your filters.</div>';
    }}
    document.getElementById('syncsContent').innerHTML=h;return;
  }}

  var pg=paginateSlice(syncPage,PAGE_SYNC,filtered.length);
  syncPage=pg.page;
  var slice=filtered.slice(pg.sliceStart,pg.sliceEnd);

  h+=showingLine(filtered.length,pool.length,'',all.length);
  h+='<table><thead><tr><th>Name</th><th>Resource Type</th><th>Organization</th><th>Sync status</th><th class="num">Last job ID</th></tr></thead><tbody>';
  slice.forEach(function(s){{
    h+='<tr>';
    h+='<td>'+objD(s.name,s.org||'Global / Unscoped',null,s.tid)+'</td>';
    h+='<td>'+esc(syncTypeLabel(s.type))+'</td>';
    h+='<td>'+esc(s.org||'—')+'</td>';
    h+='<td><span style="'+syncStatusStyle(s.status,s.failed)+'">'+esc(s.status||'—')+'</span></td>';
    h+='<td class="num">'+(s.jobId!=null?fmt(s.jobId):'<span style="color:var(--fg2)">—</span>')+'</td>';
    h+='</tr>';
  }});
  h+='</tbody></table>';

  if(pg.pages>1){{
    h+=renderPager(pg.page,pg.pages,filtered.length,'syncPage--;renderSyncs()','syncPage++;renderSyncs()');
  }}

  document.getElementById('syncsContent').innerHTML=h;
  restoreKeptFocus();
}}

/* ── Tab 5: Field Mismatches (grouped by object) ── */
var fldPage=1,fldType='all',fldOrg='',fldSearch='',fldField='all',fldSort='type_name';
var _fldOrgInit=false;
function renderFields(){{
  seedScopedOrgOnce('_fldOrgInit',function(){{return fldOrg}},function(v){{fldOrg=v}});
  var all=[];
  D.per_type.forEach(function(t){{
    (t.t3_field_parity.findings||[]).forEach(function(f){{
      all.push({{type:t.resource_type,name:f.name,org:f.organization,sid:f.source_id,tid:f.target_id,field:f.field,sv:f.source_value,tv:f.target_value,tier:f.tier}});
    }});
  }});

  var fmmObj=0;
  D.per_type.forEach(function(t){{
    fmmObj+=t.t3_field_parity.mismatching||0;
  }});
  var isLive = D.metadata && D.metadata.mode === 'validate-live';
  var t3NotRun = !isLive;
  var h='<h2>Field Parity &mdash; T3</h2>';
  if(isLive){{
    h+='<div class="cards" style="grid-template-columns:repeat(2,minmax(140px,1fr));max-width:420px">';
    h+='<div class="card"><div class="v'+(all.length===0?' ok':' warn')+'">'+fmt(all.length)+'</div><div class="l">Mismatched fields</div></div>';
    h+='<div class="card"><div class="v'+(fmmObj===0?' ok':' warn')+'">'+fmt(fmmObj)+'</div><div class="l">Mismatched objects</div></div>';
    h+='</div>';
  }}

  if(t3NotRun){{
    h+='<div class="callout callout-warn"><strong>Not run:</strong> Field changes (T3) are only available with <code>--live</code>.</div>';
  }}

  if(!all.length){{
    if(isLive){{
      h+='<div class="callout callout-pass">No field mismatches detected.</div>';
    }}
    document.getElementById('fieldsContent').innerHTML=h;return;
  }}

  var types=allTypes();
  var fieldOpts=[];
  all.forEach(function(f){{
    if(fieldOpts.indexOf(f.field)<0)fieldOpts.push(f.field);
  }});
  fieldOpts.sort();

  h+='<div class="filter-bar">';
  h+='<select onchange="fldType=this.value;fldPage=1;renderFields()">';
  h+='<option value="all"'+(fldType==='all'?' selected':'')+'>All types</option>';
  types.forEach(function(t){{h+='<option value="'+esc(t)+'"'+(fldType===t?' selected':'')+'>'+esc(t)+'</option>'}});
  h+='</select>';
  h+='<select onchange="fldField=this.value;fldPage=1;renderFields()" style="max-width:220px">';
  h+='<option value="all"'+(fldField==='all'?' selected':'')+'>All fields</option>';
  fieldOpts.forEach(function(fn){{h+='<option value="'+esc(fn)+'"'+(fldField===fn?' selected':'')+'>'+esc(fn)+'</option>'}});
  h+='</select>';
  h+=orgFilterHtml('flt-org-fields',fldOrg,'fldOrg','fldPage=1;renderFields()');
  h+='<select onchange="fldSort=this.value;fldPage=1;renderFields()">';
  h+='<option value="type_name"'+(fldSort==='type_name'?' selected':'')+'>Sort: type, name</option>';
  h+='<option value="name"'+(fldSort==='name'?' selected':'')+'>Sort: name</option>';
  h+='<option value="type"'+(fldSort==='type'?' selected':'')+'>Sort: type</option>';
  h+='<option value="org"'+(fldSort==='org'?' selected':'')+'>Sort: org</option>';
  h+='<option value="count_desc"'+(fldSort==='count_desc'?' selected':'')+'>Sort: most changed fields</option>';
  h+='</select>';
  h+=searchFilterHtml('flt-name-fields',fldSearch,'Search object name...','fldSearch=this.value;fldPage=1;renderFields()','fldSearch=\\'\\';fldPage=1;renderFields()');
  if(fldType!=='all'||fldOrg||fldSearch||fldField!=='all')h+='<button onclick="fldType=\\'all\\';fldOrg=\\'\\';fldSearch=\\'\\';fldField=\\'all\\';fldPage=1;renderFields()">Clear</button>';
  h+='</div>';

  var pool=all.filter(function(f){{
    if(fldType!=='all'&&f.type!==fldType)return false;
    if(fldField!=='all'&&f.field!==fldField)return false;
    if(fldOrg&&f.org!==fldOrg)return false;
    return true;
  }});
  var filtered=pool.filter(function(f){{
    if(fldSearch&&f.name.toLowerCase().indexOf(fldSearch.toLowerCase())<0)return false;
    return true;
  }});

  function fieldObjKey(f){{
    return f.type+'\\0'+(f.sid!=null?f.sid:f.name)+'\\0'+f.name;
  }}
  var poolObjKeys={{}};
  pool.forEach(function(f){{poolObjKeys[fieldObjKey(f)]=true;}});
  var allObjKeys={{}};
  all.forEach(function(f){{allObjKeys[fieldObjKey(f)]=true;}});

  // Group all field changes for the same object into one card
  var byObj={{}};
  var order=[];
  filtered.forEach(function(f){{
    var key=fieldObjKey(f);
    if(!byObj[key]){{
      byObj[key]={{type:f.type,name:f.name,org:f.org,sid:f.sid,tid:f.tid,tier:f.tier,fields:[]}};
      order.push(key);
    }}
    byObj[key].fields.push(f);
  }});
  order.sort(function(ka,kb){{
    var a=byObj[ka],b=byObj[kb];
    var by=sortByNameTypeOrg(a,b,fldSort);
    if(by)return by;
    if(fldSort==='count_desc')return (b.fields.length-a.fields.length)||cmpLocale(a.name,b.name);
    if(fldSort==='type_name'){{
      var d=cmpLocale(a.type,b.type);if(d)return d;
    }}
    return cmpLocale(a.name,b.name);
  }});

  var pg=paginateSlice(fldPage,PAGE_FIELDS,order.length);
  fldPage=pg.page;
  var slice=order.slice(pg.sliceStart,pg.sliceEnd);

  var poolObjCount=Object.keys(poolObjKeys).length;
  var allObjCount=Object.keys(allObjKeys).length;
  var fieldsLine='Showing '+fmt(order.length)+' objects ('+fmt(filtered.length)+' field changes) of '+fmt(poolObjCount)+' objects ('+fmt(pool.length)+' field changes)';
  if(all.length!==pool.length||(filtered.length<pool.length&&pool.length<all.length)){{
    fieldsLine+=' &middot; '+fmt(allObjCount)+' objects ('+fmt(all.length)+' field changes) total';
  }}
  h+='<div style="font-size:.82rem;color:var(--fg2);margin:.3rem 0">'+fieldsLine+'</div>';

  if(!slice.length){{
    h+='<div class="empty-msg" style="padding:1.5rem;text-align:center">No field changes match your filters.</div>';
  }}

  slice.forEach(function(key,i){{
    var g=byObj[key];
    var num=pg.sliceStart+i+1;
    var tSafe=esc(g.type).replace(/'/g,"\\\\'"), nSafe=esc(g.name).replace(/'/g,"\\\\'"), oSafe=esc(g.org||'').replace(/'/g,"\\\\'");
    h+='<div class="finding clickable" onclick="jumpToTypeObject(\\''+tSafe+'\\',\\''+nSafe+'\\',\\''+oSafe+'\\')"><div class="finding-hd">';
    h+='<span><strong>#'+num+'</strong> &mdash; <code>'+esc(g.type)+'</code> &middot; '+fmt(g.fields.length)+' field'+(g.fields.length===1?'':'s')+'</span>';
    h+='<span>'+esc(g.tier||'')+'</span></div>';
    h+='<div class="finding-bd">';
    h+='<div style="margin-bottom:.5rem">'+objD(g.name,g.org,g.sid,g.tid)+'</div>';
    h+='<table><thead><tr><th>Field</th><th>Source (2.4)</th><th>Target (2.6)</th></tr></thead><tbody>';
    g.fields.forEach(function(f){{
      h+='<tr>';
      h+='<td><code>'+esc(f.field)+'</code></td>';
      h+='<td class="src-val">'+esc(truncVal(f.sv))+'</td>';
      h+='<td class="tgt-val">'+esc(truncVal(f.tv))+'</td>';
      h+='</tr>';
    }});
    h+='</tbody></table></div></div>';
  }});

  if(pg.pages>1){{
    h+=renderPager(pg.page,pg.pages,order.length,'fldPage--;renderFields()','fldPage++;renderFields()','objects');
  }}

  document.getElementById('fieldsContent').innerHTML=h;
  restoreKeptFocus();
}}

/* ── Tab 6: Hosts (T4) ── */
var hostInvMode='mismatch',hostInvName='',hostSort='delta';
function renderHosts(){{
  var H=HOST_T4||{{}};
  var invs=H.inventories||[];
  var h='<h2>Host Validation &mdash; T4</h2>';
  if(!H.ran){{
    h+='<div class="callout callout-info">Host sampling not run for this validation.</div>';
    document.getElementById('hostsContent').innerHTML=h;return;
  }}
  h+='<h3>Existence (100% coverage)</h3>';
  h+='<div class="cards" style="grid-template-columns:repeat(4,1fr);">';
  h+='<div class="card"><div class="v">'+fmt(H.total_hosts_source||0)+'</div><div class="l">Source hosts</div></div>';
  h+='<div class="card"><div class="v">'+fmt(H.total_hosts_target||0)+'</div><div class="l">Target hosts</div></div>';
  h+='<div class="card"><div class="v ok">'+fmt(H.matched_hosts||0)+'</div><div class="l">Matched</div></div>';
  h+='<div class="card"><div class="v'+((H.missing_hosts||0)===0?' ok':' bad')+'">'+fmt(H.missing_hosts||0)+'</div><div class="l">Missing</div></div>';
  h+='</div>';
  h+='<h3>Per-Inventory Count Parity</h3>';
  h+='<div class="cards" style="grid-template-columns:repeat(3,1fr);">';
  h+='<div class="card"><div class="v">'+fmt(H.inventories_checked||0)+'</div><div class="l">Inventories</div></div>';
  h+='<div class="card"><div class="v ok">'+fmt(H.matching||0)+'</div><div class="l">Count match</div></div>';
  h+='<div class="card"><div class="v'+((H.mismatching||0)===0?' ok':' bad')+'">'+fmt(H.mismatching||0)+'</div><div class="l">Count mismatch</div></div>';
  h+='</div>';

  var names=invs.map(function(i){{return i.inventory}}).filter(Boolean).sort(function(a,b){{return a.localeCompare(b)}});
  h+='<div class="filter-bar">';
  h+='<select onchange="hostInvMode=this.value;renderHosts()">';
  h+='<option value="mismatch"'+(hostInvMode==='mismatch'?' selected':'')+'>Mismatching only</option>';
  h+='<option value="all"'+(hostInvMode==='all'?' selected':'')+'>All inventories</option>';
  h+='</select>';
  h+='<select onchange="hostSort=this.value;renderHosts()">';
  h+='<option value="delta"'+(hostSort==='delta'?' selected':'')+'>Sort: biggest delta</option>';
  h+='<option value="name"'+(hostSort==='name'?' selected':'')+'>Sort: inventory name</option>';
  h+='<option value="source"'+(hostSort==='source'?' selected':'')+'>Sort: source count</option>';
  h+='<option value="target"'+(hostSort==='target'?' selected':'')+'>Sort: target count</option>';
  h+='</select>';
  h+=comboFilterHtml(
    'flt-host-inv',
    hostInvName,
    'hostInvName',
    'renderHosts()',
    names,
    {{
      placeholder:'Inventory (type or pick)...',
      allLabel:'All inventories',
      emptyMsg:'No matching inventories',
      clearTitle:'Clear inventory filter',
      wide:true
    }}
  );
  if(hostInvMode!=='mismatch'||hostInvName)h+='<button onclick="hostInvMode=\\'mismatch\\';hostInvName=\\'\\';renderHosts()">Clear</button>';
  h+='</div>';

  var pool=invs.filter(function(inv){{
    if(hostInvMode==='mismatch'&&!(inv.delta))return false;
    return true;
  }});
  var filtered=pool.filter(function(inv){{
    if(hostInvName&&String(inv.inventory||'').toLowerCase().indexOf(hostInvName.toLowerCase())<0)return false;
    return true;
  }});
  filtered.sort(function(a,b){{
    if(hostSort==='name')return String(a.inventory||'').localeCompare(String(b.inventory||''));
    if(hostSort==='source')return (b.source_count||0)-(a.source_count||0)||String(a.inventory||'').localeCompare(String(b.inventory||''));
    if(hostSort==='target')return (b.target_count||0)-(a.target_count||0)||String(a.inventory||'').localeCompare(String(b.inventory||''));
    var da=Math.abs(a.delta||0),db=Math.abs(b.delta||0);if(db!==da)return db-da;
    return String(a.inventory||'').localeCompare(String(b.inventory||''));
  }});

  h+=showingLine(filtered.length,pool.length,'inventories',invs.length);
  h+='<table><thead><tr><th>Inventory</th><th class="num">Source</th><th class="num">Target</th><th class="num">Delta</th></tr></thead><tbody>';
  if(!filtered.length){{
    h+='<tr><td colspan="4" class="empty-msg">'+(hostInvMode==='mismatch'&&!hostInvName?'All inventory counts match.':'No inventories match your filters.')+'</td></tr>';
  }}
  filtered.forEach(function(inv){{
    h+='<tr><td><strong>'+esc(inv.inventory)+'</strong> <span class="ids">[src:'+(inv.source_id!=null?inv.source_id:'—')+' → tgt:'+(inv.target_id!=null?inv.target_id:'—')+']</span></td>';
    h+='<td class="num">'+fmt(inv.source_count||0)+'</td>';
    h+='<td class="num">'+fmt(inv.target_count||0)+'</td>';
    h+='<td class="num" style="color:'+(inv.delta?'var(--fail)':'var(--pass)')+'">'+(inv.delta>0?'+':'')+(inv.delta||0)+'</td></tr>';
  }});
  h+='</tbody></table>';

  h+='<h3>Field Parity (Stratified Sample)</h3>';
  h+='<div class="cards" style="grid-template-columns:repeat(4,1fr);">';
  h+='<div class="card"><div class="v">'+fmt(H.sample_size||0)+'</div><div class="l">Sampled</div></div>';
  h+='<div class="card"><div class="v">'+fmt(H.inventories_checked||0)+'</div><div class="l">Inventories</div></div>';
  h+='<div class="card"><div class="v'+((H.field_mismatches_in_sample||0)===0?' ok':' warn')+'">'+fmt(H.field_mismatches_in_sample||0)+'</div><div class="l">Mismatches</div></div>';
  h+='<div class="card"><div class="v">'+esc(H.confidence||'')+'</div><div class="l">Confidence</div></div>';
  h+='</div>';

  document.getElementById('hostsContent').innerHTML=h;
  restoreKeptFocus();
}}

init();
</script>
</body>
</html>"""

    return html


def slug_report_path_segment(name: str) -> str:
    """Filesystem-safe single path segment (org or resource type name)."""
    cleaned = "".join(
        c if c.isalnum() or c in "-_" else "-"
        for c in (name or "").strip()
    )
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-_") or "unnamed"


def resolve_validate_report_dir(
    base_dir: str | Path,
    *,
    live: bool,
    organizations: list[str] | None = None,
    resource_type: str | None = None,
    day: str | None = None,
) -> Path:
    """Build reports/validate/<date>/<live|database>/[org|multi]/[type]/.

    Same calendar day overwrites are intentional. Multi-org runs write a
    combined report under ``multi/``; per-org reports use the same
    ``<OrgSlug>/`` paths as a single ``--orgs`` run (see
    ``write_org_scoped_validation_reports``).
    """
    from datetime import datetime, timezone

    root = Path(base_dir)
    if day is None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mode = "live" if live else "database"
    path = root / "validate" / day / mode

    orgs = [o for o in (organizations or []) if o]
    if len(orgs) == 1:
        path = path / slug_report_path_segment(orgs[0])
    elif len(orgs) > 1:
        path = path / "multi"

    if resource_type and str(resource_type).strip():
        path = path / slug_report_path_segment(str(resource_type).strip())

    return path


def write_validation_report(
    result: ValidationResult,
    output_dir: str,
    json_filename: str = "report.json",
    html_filename: str = "report.html",
    field_data: dict | None = None,
) -> tuple[str, str]:
    os.makedirs(output_dir, mode=0o700, exist_ok=True)
    json_path = os.path.join(output_dir, json_filename)
    export_validation_json(result, json_path)
    html_path = os.path.join(output_dir, html_filename)
    _write_secure(html_path, generate_validation_html(result, field_data=field_data))
    return json_path, html_path
