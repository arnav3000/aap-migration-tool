"""Report formatters for dependency analysis results."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aap_migration.analysis.dependency_analyzer import (
        GlobalDependencyReport,
        OrgDependencyReport,
    )


def format_summary_report(report: GlobalDependencyReport) -> str:
    """Format global dependency analysis as summary view (Format A).

    Args:
        report: Global dependency report

    Returns:
        Formatted report string
    """
    lines = []

    # Header
    lines.append("╭─────────────────────────────────────────────────────────────────╮")
    lines.append("│            Cross-Organization Dependency Analysis               │")
    lines.append("│                    AAP Source Environment                       │")
    lines.append("╰─────────────────────────────────────────────────────────────────╯")
    lines.append("")
    lines.append(f"Source: {report.source_url}")
    lines.append(f"Analyzed: {report.total_organizations} organizations")
    lines.append(f"Date: {report.analysis_date.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # Independent Organizations
    if report.independent_orgs:
        lines.append("╭─ Independent Organizations (Can migrate standalone) ────────────╮")
        for org_name in report.independent_orgs:
            org_report = report.org_reports[org_name]
            lines.append(f"│ ✓ {org_name:<63}│")
            lines.append(
                f"│   - {org_report.resource_count} resources{' ' * (57 - len(str(org_report.resource_count)))}│"
            )
            lines.append(f"│   - No external dependencies{' ' * 36}│")
            lines.append("│                                                                  │")
        lines.append("╰─────────────────────────────────────────────────────────────────╯")
        lines.append("")

    # Organizations with Cross-Org Dependencies
    if report.dependent_orgs:
        lines.append("╭─ Organizations with Cross-Org Dependencies ─────────────────────╮")
        lines.append("│                                                                  │")
        for org_name in report.dependent_orgs:
            org_report = report.org_reports[org_name]
            lines.append(f"│ ⚠  {org_name:<63}│")
            lines.append("│    Depends on:                                                   │")

            for dep_org, resources in org_report.dependencies.items():
                # Group resources by type
                by_type: dict[str, int] = {}
                for res in resources:
                    by_type[res.resource_type] = by_type.get(res.resource_type, 0) + 1

                lines.append(f"│      • {dep_org:<58}│")
                for rtype, count in sorted(by_type.items()):
                    plural = "s" if count > 1 else ""
                    lines.append(
                        f"│          - {count} {rtype}{plural}{' ' * (53 - len(str(count)) - len(rtype) - len(plural))}│"
                    )

            lines.append("│                                                                  │")

        lines.append("╰─────────────────────────────────────────────────────────────────╯")
        lines.append("")

    # Migration Recommendations
    lines.append("╭─ Migration Recommendations ─────────────────────────────────────╮")
    lines.append("│                                                                  │")

    # Group phases
    if report.migration_phases:
        lines.append("│ 📋 Migration Phases:                                             │")
        lines.append("│                                                                  │")
        for phase in report.migration_phases:
            phase_num = phase["phase"]
            orgs = phase["orgs"]
            desc = phase["description"]

            lines.append(f"│    Phase {phase_num}: {desc[:55]}{' ' * max(0, 55 - len(desc))}│")
            for org in orgs:
                lines.append(f"│      • {org:<58}│")
            lines.append("│                                                                  │")

    lines.append("│ 💡 Recommended Migration Order:                                  │")
    for i, org_name in enumerate(report.migration_order, 1):
        org_report = report.org_reports[org_name]
        if org_report.has_cross_org_deps:
            dep_str = f" (needs: {', '.join(org_report.required_migrations_before)})"
            if len(dep_str) > 40:
                dep_str = f" (needs: {len(org_report.required_migrations_before)} orgs)"
        else:
            dep_str = " (no deps)"

        line = f"│    {i}. {org_name}{dep_str}"
        line += " " * (66 - len(line)) + "│"
        lines.append(line)

    lines.append("│                                                                  │")

    # Warning
    if report.dependent_orgs:
        lines.append(
            f"│ ⚠️  WARNING: {len(report.dependent_orgs)} organizations have cross-org dependencies{' ' * (7 - len(str(len(report.dependent_orgs))))}│"
        )
        lines.append("│    Migration must follow dependency order to avoid failures     │")

    lines.append("╰─────────────────────────────────────────────────────────────────╯")
    lines.append("")

    return "\n".join(lines)


def format_detailed_report(org_report: OrgDependencyReport) -> str:
    """Format single organization dependency analysis (Format B).

    Args:
        org_report: Organization dependency report

    Returns:
        Formatted report string
    """
    lines = []

    # Header
    lines.append("╭─────────────────────────────────────────────────────────────────╮")
    lines.append(f"│ Organization: {org_report.org_name:<52}│")
    lines.append("╰─────────────────────────────────────────────────────────────────╯")
    lines.append("")
    lines.append(f"Resources: {org_report.resource_count} total")
    lines.append("")

    if not org_report.has_cross_org_deps:
        lines.append("✓ No cross-organization dependencies")
        lines.append("✓ Can be migrated standalone")
        lines.append("")
        lines.append("Recommendation:")
        lines.append(f'  aap-bridge migrate -o "{org_report.org_name}"')
        return "\n".join(lines)

    lines.append("Cross-Organization Dependencies:")
    lines.append("")

    # For each dependency org
    for dep_org, resources in org_report.dependencies.items():
        lines.append("┌─────────────────────────────────────────────────────────────────┐")
        lines.append(f"│ Depends on: {dep_org:<52}│")
        lines.append("├─────────────────────────────────────────────────────────────────┤")
        lines.append("│                                                                  │")

        # Group by resource type
        by_type: dict[str, list] = {}
        for res in resources:
            if res.resource_type not in by_type:
                by_type[res.resource_type] = []
            by_type[res.resource_type].append(res)

        # Show each resource
        for rtype in sorted(by_type.keys()):
            for res in by_type[rtype]:
                # Resource header
                res_line = f"│ {rtype.capitalize()}: {res.resource_name} (ID: {res.resource_id})"
                res_line += " " * (66 - len(res_line)) + "│"
                lines.append(res_line)

                # Show what requires it
                lines.append("│   Required by:                                                   │")
                for usage in res.required_by:
                    usage_line = f'│     • {usage["type"].replace("_", " ").title()}: "{usage["name"]}" (ID: {usage["id"]})'
                    if len(usage_line) > 65:
                        usage_line = usage_line[:62] + "...│"
                    else:
                        usage_line += " " * (66 - len(usage_line)) + "│"
                    lines.append(usage_line)
                lines.append("│                                                                  │")

        # Summary for this dependency org
        summary_parts = []
        for rtype, res_list in sorted(by_type.items()):
            plural = "s" if len(res_list) > 1 else ""
            summary_parts.append(f"{len(res_list)} {rtype}{plural}")

        lines.append("├─────────────────────────────────────────────────────────────────┤")
        summary = f"│ Total: {', '.join(summary_parts)}"
        summary += " " * (66 - len(summary)) + "│"
        lines.append(summary)
        lines.append("└─────────────────────────────────────────────────────────────────┘")
        lines.append("")

    # Recommendation
    lines.append("Recommendation:")
    lines.append("  ⚠️  Cannot migrate standalone - must migrate dependencies first")
    lines.append("")
    lines.append("  Required migrations (in order):")
    for i, dep_org in enumerate(org_report.required_migrations_before, 1):
        lines.append(f'    {i}. aap-bridge migrate -o "{dep_org}"')
    lines.append(
        f'    {len(org_report.required_migrations_before) + 1}. aap-bridge migrate -o "{org_report.org_name}"'
    )

    return "\n".join(lines)
