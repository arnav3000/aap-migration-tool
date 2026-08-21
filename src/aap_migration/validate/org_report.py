"""Slice a validation result into per-organization reports.

One ``run_validation()`` pass; filter in memory; write N HTML/JSON reports.
No extra API calls.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from aap_migration.validate.common import (
    apply_migration_buckets,
    build_executive_summary,
    belongs_to_org,
    count_explained_from_missing,
    int_dict_key,
)
from aap_migration.validate.models import (
    OrgValidationSummary,
    PerInventoryCountParity,
    PerTypeResult,
    T1Counts,
    T2Existence,
    T3FieldParity,
    T4HostSampling,
    ValidationResult,
)


def filter_field_data_for_org(
    field_data: dict | None,
    result: ValidationResult,
) -> dict | None:
    """Keep live field-compare rows whose source id is in the sliced inventory."""
    if not field_data:
        return None
    sids_by_type: dict[str, set[int]] = {}
    for rtype, entries in result.object_inventory.items():
        sids = {e.source_id for e in entries if e.source_id is not None}
        if sids:
            sids_by_type[rtype] = sids
    # Also include source ids from findings (matched objects with field diffs)
    for pt in result.per_type:
        for ff in pt.t3_field_parity.findings:
            if ff.source_id is None:
                continue
            sids_by_type.setdefault(pt.resource_type, set()).add(ff.source_id)

    out: dict[str, dict] = {}
    for rtype, td in field_data.items():
        want = sids_by_type.get(rtype)
        if not want:
            continue
        cols = td.get("c") or []
        src = td.get("s") or {}
        tgt = td.get("t") or {}
        new_s = {
            k: v for k, v in src.items()
            if int_dict_key(k) in want
        }
        new_t = {
            k: v for k, v in tgt.items()
            if int_dict_key(k) in want
        }
        if new_s or new_t:
            out[rtype] = {"c": cols, "s": new_s, "t": new_t}
    return out or None


def filter_validation_result_for_org(
    result: ValidationResult,
    org_name: str,
) -> ValidationResult:
    """Return a ValidationResult limited to a single organization."""
    meta = replace(
        result.metadata,
        organizations=[org_name],
    )
    org_sum = result.per_org.get(org_name)
    if org_sum is None:
        org_sum = OrgValidationSummary(org_name=org_name)
    per_org = {org_name: org_sum}
    rollup_by_type = {r.resource_type: r for r in org_sum.per_type}

    object_inventory: dict[str, list] = {}
    for rtype, entries in result.object_inventory.items():
        object_inventory[rtype] = [
            e for e in entries if belongs_to_org(e, org_name)
        ]

    per_type: list[PerTypeResult] = []
    for pt in result.per_type:
        rtype = pt.resource_type
        missing_details = [
            md for md in pt.t2_existence.missing_details
            if belongs_to_org(md, org_name)
        ]
        extra_details = [
            ed for ed in pt.t2_existence.extra_details
            if belongs_to_org(ed, org_name)
        ]
        findings = [
            ff for ff in pt.t3_field_parity.findings
            if belongs_to_org(ff, org_name)
        ]

        rollup = rollup_by_type.get(rtype)
        entries = object_inventory.get(rtype, [])
        if rollup is not None:
            source = rollup.source
            matched = rollup.matched
            missing = rollup.missing
        else:
            source = len(entries)
            matched = sum(1 for e in entries if e.status == "completed")
            missing = len(missing_details)

        extra = len(extra_details)
        # Prefer live-style target ≈ matched + extras when rollup has no target
        target = matched + extra
        if rollup is not None and rollup.target:
            target = rollup.target

        expl_f, expl_s, unexplained = count_explained_from_missing(missing_details)

        mismatch_sids = {
            ff.source_id for ff in findings if ff.source_id is not None
        }
        mismatching = len(mismatch_sids) if mismatch_sids else (1 if findings else 0)
        if rollup is not None and rollup.field_mismatches:
            mismatching = rollup.field_mismatches
        compared = matched if result.metadata.mode == "validate-live" else 0
        matching = max(0, compared - mismatching)

        # Drop types with nothing for this org (keeps report focused)
        if (
            source == 0 and missing == 0 and extra == 0
            and not findings and not entries
        ):
            continue

        per_type.append(PerTypeResult(
            resource_type=rtype,
            display_name=pt.display_name,
            t1_counts=T1Counts(
                source=source,
                target=target,
                delta=source - target,
                explained_failures=expl_f,
                explained_skips=expl_s,
                unexplained=unexplained,
            ),
            t2_existence=T2Existence(
                matched=matched,
                missing_on_target=missing,
                extra_on_target=extra,
                missing_details=missing_details,
                extra_details=extra_details,
            ),
            t3_field_parity=T3FieldParity(
                compared=compared,
                matching=matching,
                mismatching=mismatching,
                findings=findings,
            ),
        ))

    t4 = _filter_t4_for_org(result.t4_host_sampling, object_inventory)

    sync_entries = [
        entry for entry in result.sync_entries
        if belongs_to_org(entry, org_name)
    ]
    workflow_comparisons = [
        item for item in result.workflow_comparisons
        if not item.org or item.org == org_name
    ]

    per_type = apply_migration_buckets(per_type, object_inventory)

    return ValidationResult(
        metadata=meta,
        executive_summary=build_executive_summary(
            per_type,
            sync_failed=sum(1 for entry in sync_entries if entry.failed),
        ),
        per_type=per_type,
        per_org=per_org,
        object_inventory=object_inventory,
        t4_host_sampling=t4,
        # Org-scoped runs skip auditor; keep empty on slices too
        auditor_cross_check=result.auditor_cross_check,
        sync_entries=sync_entries,
        workflow_comparisons=workflow_comparisons,
    )


def _filter_t4_for_org(
    t4: T4HostSampling,
    object_inventory: dict[str, list],
) -> T4HostSampling:
    org_inv_names = {
        e.name for e in object_inventory.get("inventories", []) if e.name
    }
    details = [
        d for d in t4.per_inventory_count_parity.details
        if d.inventory in org_inv_names
    ]
    matching = sum(1 for d in details if d.delta == 0)
    mismatching = sum(1 for d in details if d.delta != 0)

    host_entries = object_inventory.get("hosts", [])
    matched_hosts = sum(1 for e in host_entries if e.status == "completed")
    missing_hosts = sum(1 for e in host_entries if e.status != "completed")

    return T4HostSampling(
        total_hosts_source=len(host_entries),
        total_hosts_target=matched_hosts,  # best available without re-fetch
        matched_hosts=matched_hosts,
        missing_hosts=missing_hosts,
        inventories_checked=len(details) if details else len(org_inv_names),
        sample_size=0,
        sample_methodology=t4.sample_methodology,
        confidence=t4.confidence,
        margin_of_error=t4.margin_of_error,
        field_mismatches_in_sample=0,
        per_inventory_count_parity=PerInventoryCountParity(
            matching=matching,
            mismatching=mismatching,
            details=details,
        ),
    )


def write_org_scoped_validation_reports(
    result: ValidationResult,
    *,
    base_dir: str | Path,
    live: bool,
    organizations: list[str],
    resource_type: str | None = None,
    field_data: dict | None = None,
    day: str | None = None,
) -> list[tuple[str, str, str]]:
    """Write combined report plus per-org reports for multi --orgs.

    Layout (same calendar day / mode)::

        …/<live|database>/multi/report.html     # combined (2+ orgs)
        …/<live|database>/<OrgA>/report.html    # per org (same path as
        …/<live|database>/<OrgB>/report.html    # a single --orgs run)

    Single-org runs write only ``…/<Org>/report.html``.

    Returns list of (org_label, json_path, html_path). Combined is first
    with org_label ``combined`` when written.
    """
    from aap_migration.validate.report import (
        resolve_validate_report_dir,
        write_validation_report,
    )

    written: list[tuple[str, str, str]] = []
    orgs = [o for o in (organizations or []) if o]

    # Ensure combined metadata lists all selected orgs (header / callout)
    combined = result
    if orgs and list(result.metadata.organizations or []) != list(orgs):
        combined = replace(
            result,
            metadata=replace(result.metadata, organizations=list(orgs)),
        )

    if len(orgs) <= 1:
        report_dir = resolve_validate_report_dir(
            base_dir,
            live=live,
            organizations=orgs or None,
            resource_type=resource_type,
            day=day,
        )
        json_path, html_path = write_validation_report(
            combined,
            str(report_dir),
            field_data=field_data,
        )
        written.append(("combined", json_path, html_path))
        return written

    # Combined multi-org report
    combined_dir = resolve_validate_report_dir(
        base_dir,
        live=live,
        organizations=orgs,
        resource_type=resource_type,
        day=day,
    )
    json_path, html_path = write_validation_report(
        combined,
        str(combined_dir),
        field_data=field_data,
    )
    written.append(("combined", json_path, html_path))

    # Per-org reports at the same paths used for a single --orgs run
    for org_name in orgs:
        sliced = filter_validation_result_for_org(combined, org_name)
        sliced_fd = filter_field_data_for_org(field_data, sliced)
        org_dir = resolve_validate_report_dir(
            base_dir,
            live=live,
            organizations=[org_name],
            resource_type=resource_type,
            day=day,
        )
        j, h = write_validation_report(
            sliced,
            str(org_dir),
            field_data=sliced_fd,
        )
        written.append((org_name, j, h))

    return written
