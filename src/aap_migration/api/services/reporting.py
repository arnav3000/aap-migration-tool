"""Validate/analysis/reporting background workers."""

from __future__ import annotations

import asyncio
from typing import Any

from aap_migration.api.jobs import JobRecord
from aap_migration.api.services._core import (
    _relativize,
    call_command,
    chained_ctx,
    parse_organizations,
)


# -- validate ---------------------------------------------------------------
def run_validate(job: JobRecord) -> dict[str, Any]:
    """Post-migration validation (mirrors ``validate``)."""
    from aap_migration.validate.org_report import write_org_scoped_validation_reports
    from aap_migration.validate.report import resolve_validate_report_dir
    from aap_migration.validate.runner import run_validation

    with chained_ctx(job) as (ctx, config, workdir, params):
        # Schema (ValidateRequest) owns the skip_hosts/hosts exclusion; kept
        # here only for direct worker calls that bypass HTTP validation.
        if params.get("skip_hosts") and params.get("resource_type") == "hosts":
            raise ValueError("--skip-hosts conflicts with resource_type=hosts")
        # Single home for org spellings (orgs/organizations/organization);
        # None = all. parse_orgs_arg remains the comma-split primitive used
        # inside parse_organizations for str values.
        organizations = parse_organizations(dict(params))

        async def _main() -> Any:
            target_client = ctx.target_client if params.get("live") else None
            return await run_validation(
                config=config,
                migration_state=ctx.migration_state,
                target_client=target_client,
                live=bool(params.get("live", False)),
                resource_type=params.get("resource_type"),
                skip_hosts=bool(params.get("skip_hosts", False)),
                organizations=organizations,
            )

        result, field_data = asyncio.run(_main())
        written = write_org_scoped_validation_reports(
            result,
            base_dir=workdir / "reports",
            live=bool(params.get("live", False)),
            organizations=organizations or [],
            resource_type=params.get("resource_type"),
            field_data=field_data,
        )
        resolve_validate_report_dir(
            workdir / "reports",
            live=bool(params.get("live", False)),
            organizations=organizations,
            resource_type=params.get("resource_type"),
        )
    summary = {
        "types": len(result.per_type),
        "source_objects": sum(t.t1_counts.source for t in result.per_type),
        "target_objects": sum(t.t1_counts.target for t in result.per_type),
        "missing": result.executive_summary.total_missing_on_target,
        "field_mismatches": result.executive_summary.total_field_mismatches,
        "verdict": result.executive_summary.verdict,
    }
    return {
        "message": "Validation complete",
        "summary": summary,
        "reports": _relativize(
            [{"label": label, "html": html} for label, _, html in written], workdir
        ),
    }


# -- analysis ------------------------------------------------------------------
def run_analyze_dependencies(job: JobRecord) -> dict[str, Any]:
    """Cross-org dependency analysis (mirrors ``analyze-dependencies``)."""
    from aap_migration.analysis.dependency_analyzer import CrossOrgDependencyAnalyzer
    from aap_migration.analysis.html_report import generate_html_report
    from aap_migration.cli.commands.analyze_dependencies import (
        serialize_global_report_to_json,
    )

    with chained_ctx(job, need="source") as (ctx, _, workdir, params):
        # Single home for org spellings; None = all (analyze_all branch).
        # Explicit [] preserved (worker below raises the scope error).
        _scoped = parse_organizations(dict(params))
        organizations = list(_scoped) if _scoped is not None else []
        analyze_all = bool(params.get("analyze_all", False))
        # Scope rules live in AnalyzeDependenciesRequest; worker re-checks for
        # direct invocation only.
        if not analyze_all and not organizations:
            raise ValueError("Must specify analyze_all=true or organizations=[...]")
        if analyze_all and organizations:
            raise ValueError("Cannot use analyze_all with organizations")

        async def _main() -> Any:
            analyzer = CrossOrgDependencyAnalyzer(ctx.source_client)
            if analyze_all:
                return await analyzer.analyze_all_organizations(), None
            reports = {}
            for org_name in organizations:
                reports[org_name] = await analyzer.analyze_organization(org_name)
            return None, reports

        global_report, org_reports = asyncio.run(_main())
        if global_report is None:
            # Build a mini global report for the requested orgs
            from datetime import datetime

            from aap_migration.analysis.dependency_analyzer import GlobalDependencyReport
            from aap_migration.analysis.dependency_graph import (
                group_into_phases,
                topological_sort,
            )

            independent = sorted([n for n, r in org_reports.items() if not r.has_cross_org_deps])
            dependent = sorted([n for n, r in org_reports.items() if r.has_cross_org_deps])
            graph = {n: r.required_migrations_before for n, r in org_reports.items()}
            order = topological_sort(graph)
            global_report = GlobalDependencyReport(
                analysis_date=datetime.now(),
                source_url=str(ctx.source_client.base_url),
                total_organizations=len(organizations),
                analyzed_organizations=list(organizations),
                independent_orgs=independent,
                dependent_orgs=dependent,
                org_reports=org_reports,
                migration_order=order,
                migration_phases=group_into_phases(graph, order),
            )
        html_content = generate_html_report(global_report)
        html_path = workdir / "reports" / "dependencies.html"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(html_content)
        json_path = workdir / "reports" / "dependencies.json"
        with open(json_path, "w", encoding="utf-8") as fh:
            fh.write(serialize_global_report_to_json(global_report))
    return {
        "message": "Dependency analysis complete",
        "migration_order": global_report.migration_order,
        "migration_phases": global_report.migration_phases,
        "independent_orgs": global_report.independent_orgs,
        "dependent_orgs": global_report.dependent_orgs,
        "html_report": str(html_path.relative_to(workdir)),
        "json_report": str(json_path.relative_to(workdir)),
    }


# -- reporting --------------------------------------------------------------------
def run_migration_report(job: JobRecord) -> dict[str, Any]:
    """Failure analysis report (mirrors ``migration-report``)."""
    with chained_ctx(job) as (ctx, _, workdir, params):
        output = str(
            workdir / "reports" / f"migration-report.{params.get('output_format', 'markdown')}"
        )
        call_command(
            "migration-report",
            ctx,
            output=output,
            resource_type=params.get("resource_type"),
            by_organization=bool(params.get("by_organization", False)),
            output_format=params.get("output_format", "markdown"),
        )
    return {"message": "Migration report complete", "report": _relativize(output, workdir)}


def run_enhanced_report(job: JobRecord) -> dict[str, Any]:
    """Enhanced org report (mirrors ``enhanced-report``)."""
    with chained_ctx(job) as (ctx, _, workdir, params):
        fmt = str(params.get("output_format", "html"))
        ext = {"html": "html", "markdown": "md", "csv": "csv"}[fmt]
        output = str(workdir / "reports" / f"org-failures-enhanced.{ext}")
        # Single home for org spellings; enhanced CLI takes a single org.
        scoped = parse_organizations(dict(params))
        if scoped is None or len(scoped) == 0:
            organization: str | None = None
        elif len(scoped) == 1:
            organization = scoped[0]
        else:
            raise ValueError(f"Enhanced report supports a single organization; got {len(scoped)}")
        call_command(
            "enhanced-report",
            ctx,
            output=output,
            resource_type=params.get("resource_type"),
            output_format=fmt,
            organization=organization,
        )
    return {"message": "Enhanced report complete", "report": _relativize(output, workdir)}


def run_project_failures(job: JobRecord) -> dict[str, Any]:
    """Project failure analysis (mirrors ``analyze-project-failures``)."""
    with chained_ctx(job) as (ctx, _, workdir, params):
        output = workdir / "reports" / "PROJECT-FAILURES-REPORT.md"
        call_command("analyze-project-failures", ctx, output=str(output))
    return {
        "message": "Project failures report complete",
        "report": _relativize(str(output), workdir),
    }
