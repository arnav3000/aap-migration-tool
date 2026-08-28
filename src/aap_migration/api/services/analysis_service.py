"""Analysis service for cross-org dependencies (Task 4 clean)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from aap_migration.analysis.dependency_analyzer import CrossOrgDependencyAnalyzer
from aap_migration.analysis.html_report import generate_html_report
from aap_migration.api.models import Connection
from aap_migration.api.services.job_service import JobService


def _serialize_global_report(report) -> dict:
    organizations = {}
    for org_name, org_report in report.org_reports.items():
        dependencies = {}
        for dep_org, dep_resources in org_report.dependencies.items():
            dependencies[dep_org] = [
                {
                    "resource_type": dep.resource_type,
                    "resource_id": dep.resource_id,
                    "resource_name": dep.resource_name,
                    "used_by": dep.required_by,
                }
                for dep in dep_resources
            ]
        organizations[org_name] = {
            "org_id": org_report.org_id,
            "resource_count": org_report.resource_count,
            "has_dependencies": org_report.has_cross_org_deps,
            "can_migrate_standalone": org_report.can_migrate_standalone,
            "required_before": org_report.required_migrations_before,
            "dependencies": dependencies,
        }
    return {
        "analysis_date": report.analysis_date.isoformat(),
        "source_url": report.source_url,
        "total_organizations": report.total_organizations,
        "analyzed_organizations": report.analyzed_organizations,
        "independent_orgs": report.independent_orgs,
        "dependent_orgs": report.dependent_orgs,
        "migration_order": report.migration_order,
        "migration_phases": report.migration_phases,
        "cycles": report.cycles,
        "organizations": organizations,
    }


class AnalysisService:
    def __init__(
        self, job_service: JobService, session_factory: sessionmaker | None = None
    ) -> None:
        self.job_service = job_service
        self.session_factory = session_factory

    def _snapshot(self, conn: Connection) -> dict:
        from aap_migration.api.crypto import decrypt_token

        try:
            token = decrypt_token(conn.token)
        except Exception:
            token = conn.token
        return {
            "id": conn.id,
            "name": conn.name,
            "url": conn.url,
            "token": token,
            "verify_ssl": conn.verify_ssl,
            "timeout": conn.timeout,
        }

    async def start_analysis(self, conn: Connection, organizations: list[str] | None = None) -> str:
        snapshot = self._snapshot(conn)
        # Normalize
        orgs = organizations or None

        async def _do(append_log) -> dict:
            from aap_migration.client.aap_source_client import AAPSourceClient
            from aap_migration.config import AAPInstanceConfig

            cfg = AAPInstanceConfig(
                url=snapshot["url"],
                token=snapshot["token"],
                verify_ssl=snapshot["verify_ssl"],
                timeout=snapshot["timeout"],
            )
            client = AAPSourceClient(config=cfg)
            analyzer = CrossOrgDependencyAnalyzer(client)
            append_log(f"Starting analysis on {snapshot['name']} orgs={orgs or 'all'}")
            try:
                if orgs:
                    if len(orgs) == 1:
                        org_report = await analyzer.analyze_organization(orgs[0])
                        # Wrap single into global for consistency
                        from aap_migration.analysis.dependency_analyzer import (
                            GlobalDependencyReport,
                        )
                        from aap_migration.analysis.dependency_graph import (
                            group_into_phases,
                            topological_sort,
                        )

                        graph = {orgs[0]: org_report.required_migrations_before}
                        migration_order = topological_sort(graph)
                        migration_phases = group_into_phases(graph, migration_order)
                        report = GlobalDependencyReport(
                            analysis_date=datetime.now(UTC),
                            source_url=str(client.base_url),
                            total_organizations=1,
                            analyzed_organizations=orgs,
                            independent_orgs=[] if org_report.has_cross_org_deps else orgs,
                            dependent_orgs=orgs if org_report.has_cross_org_deps else [],
                            org_reports={orgs[0]: org_report},
                            migration_order=migration_order,
                            migration_phases=migration_phases,
                        )
                    else:
                        # Multiple specific orgs
                        org_reports = {}
                        for oname in orgs:
                            append_log(f"Analyzing {oname}")
                            org_reports[oname] = await analyzer.analyze_organization(oname)
                        from aap_migration.analysis.dependency_analyzer import (
                            GlobalDependencyReport,
                        )
                        from aap_migration.analysis.dependency_graph import (
                            group_into_phases,
                            topological_sort,
                        )

                        independent = sorted(
                            [n for n, r in org_reports.items() if not r.has_cross_org_deps]
                        )
                        dependent = sorted(
                            [n for n, r in org_reports.items() if r.has_cross_org_deps]
                        )
                        graph = {
                            org: rep.required_migrations_before for org, rep in org_reports.items()
                        }
                        migration_order = topological_sort(graph)
                        migration_phases = group_into_phases(graph, migration_order)
                        report = GlobalDependencyReport(
                            analysis_date=datetime.now(UTC),
                            source_url=str(client.base_url),
                            total_organizations=len(orgs),
                            analyzed_organizations=orgs,
                            independent_orgs=independent,
                            dependent_orgs=dependent,
                            org_reports=org_reports,
                            migration_order=migration_order,
                            migration_phases=migration_phases,
                        )
                else:
                    report = await analyzer.analyze_all_organizations()
                serialized = _serialize_global_report(report)
                # Generate html for export
                try:
                    html = generate_html_report(report)
                except Exception:
                    html = "<html><body>report generation failed</body></html>"
                append_log(
                    f"Analysis complete orgs={serialized['total_organizations']} phases={len(serialized['migration_phases'])}"
                )
                return {"report": serialized, "html": html, "connection_id": snapshot["id"]}
            finally:
                try:
                    await client.close()
                except Exception:
                    pass

        job = await self.job_service.start_job("analysis", _do, name=f"analysis:{snapshot['name']}")
        return job.job_id
