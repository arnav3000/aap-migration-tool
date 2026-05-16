"""Service for running dependency analysis as background jobs."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from aap_migration.analysis.dependency_analyzer import GlobalDependencyReport
from aap_migration.api.models import Connection, Job  # type: ignore[attr-defined]
from aap_migration.api.services.job_service import JobService


class AnalysisService:
    def __init__(
        self,
        job_service: JobService,
        session_factory: sessionmaker[Session],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.job_service = job_service
        self.session_factory = session_factory
        self.loop = loop

    def _create_job(self, connection_id: str) -> str:
        job_id = str(uuid4())
        db = self.session_factory()
        try:
            job = Job(id=job_id, type="analysis", connection_id=connection_id, status="running")
            db.add(job)
            db.commit()
        finally:
            db.close()
        self.job_service.register_job(job_id)  # type: ignore[attr-defined]
        return job_id

    def _finish_job(
        self, job_id: str, status: str, error: str | None = None, metadata: dict | None = None
    ) -> None:
        db = self.session_factory()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                job.status = status
                job.finished_at = datetime.now(UTC)
                job.error = error
                if metadata:
                    job.job_metadata = metadata
                logs = self.job_service.get_logs_since(job_id, 0)
                job.output = logs
                db.commit()
        finally:
            db.close()

    def _update_progress(self, job_id: str, current: int, total: int, message: str) -> None:
        self.job_service.append_log(job_id, f"[{current}/{total}] {message}")  # type: ignore[attr-defined]
        db = self.session_factory()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                job.job_metadata = job.job_metadata or {}
                job.job_metadata["progress"] = {
                    "current": current,
                    "total": total,
                    "message": message,
                }
                db.commit()
        finally:
            db.close()

    def start_analysis(self, conn: Connection) -> str:
        job_id = self._create_job(conn.id)
        from aap_migration.api.crypto import decrypt_token

        snap = {
            "url": conn.url,
            "token": decrypt_token(conn.token) if conn.token else None,
            "verify_ssl": conn.verify_ssl,
            "api_prefix": conn.api_prefix,
        }

        async def _run() -> None:
            class _LogCapture(logging.Handler):
                def emit(self_, record: logging.LogRecord) -> None:
                    self.job_service.append_log(job_id, self_.format(record))  # type: ignore[attr-defined]

            capture = _LogCapture()
            capture.setLevel(logging.INFO)
            capture.setFormatter(logging.Formatter("%(message)s"))
            root = logging.getLogger()
            root.addHandler(capture)

            try:
                from aap_migration.analysis.dependency_analyzer import CrossOrgDependencyAnalyzer
                from aap_migration.client.aap_source_client import AAPSourceClient
                from aap_migration.config import AAPInstanceConfig

                base_url = snap["url"]
                if snap.get("api_prefix"):
                    base_url = f"{base_url}{snap['api_prefix']}"

                config = AAPInstanceConfig(
                    url=base_url,
                    token=snap["token"],
                    verify_ssl=snap["verify_ssl"],
                )
                client = AAPSourceClient(config=config)

                def progress_cb(current: int, total: int, message: str) -> None:
                    self._update_progress(job_id, current, total, message)

                analyzer = CrossOrgDependencyAnalyzer(
                    client,
                    progress_callback=progress_cb,
                )
                report = await analyzer.analyze_all_organizations()

                result = _serialize_report(report)
                self._finish_job(job_id, "completed", metadata=result)

            except Exception as e:
                self._finish_job(job_id, "failed", error=str(e))
            finally:
                root.removeHandler(capture)

        asyncio.run_coroutine_threadsafe(_run(), self.loop)
        return job_id


def _serialize_report(report: GlobalDependencyReport) -> dict[str, Any]:
    """Convert GlobalDependencyReport to a JSON-serializable dict with full detail."""
    org_data = {}
    for org_name, org_report in report.org_reports.items():
        deps = {}
        for dep_org, dep_resources in org_report.dependencies.items():
            deps[dep_org] = [
                {
                    "resource_type": d.resource_type,
                    "resource_id": d.resource_id,
                    "resource_name": d.resource_name,
                    "required_by": d.required_by,
                }
                for d in dep_resources
            ]

        quality = None
        if hasattr(org_report, "quality_report") and org_report.quality_report:
            qr = org_report.quality_report
            quality = {
                "quality_score": qr.quality_score,
                "duplicate_count": qr.duplicate_count,
                "duplicates": [
                    {
                        "name": d.name,
                        "resource_type": d.resource_type,
                        "count": d.count,
                        "ids": d.ids,
                        "severity": d.severity,
                        "impact": d.impact,
                        "recommendation": d.recommendation,
                    }
                    for d in qr.duplicates
                ]
                if qr.duplicates
                else [],
                "naming_pattern": {
                    "dominant_pattern": qr.naming_pattern.dominant_pattern,
                    "consistency_score": qr.naming_pattern.consistency_score,
                    "total_resources": qr.naming_pattern.total_resources,
                    "case_style": qr.naming_pattern.case_style,
                    "prefixes": qr.naming_pattern.prefixes,
                    "separators": qr.naming_pattern.separators,
                    "violations": qr.naming_pattern.violations[:20]
                    if qr.naming_pattern.violations
                    else [],
                }
                if qr.naming_pattern
                else None,
            }

        # Compute blocking info: how many other orgs does this org block?
        blocked_by_this = []
        for other_name, other_report in report.org_reports.items():
            if org_name in other_report.required_migrations_before:
                blocked_by_this.append(other_name)

        org_data[org_name] = {
            "org_id": org_report.org_id,
            "resource_count": org_report.resource_count,
            "has_cross_org_deps": org_report.has_cross_org_deps,
            "can_migrate_standalone": org_report.can_migrate_standalone,
            "required_migrations_before": org_report.required_migrations_before,
            "blocks": blocked_by_this,
            "dependencies": deps,
            "quality": quality,
            "resources": {rtype: len(rlist) for rtype, rlist in org_report.resources.items()}
            if hasattr(org_report, "resources") and org_report.resources
            else {},
        }

    # Aggregate quality summary
    quality_summary = None
    if hasattr(report, "get_quality_summary"):
        try:
            quality_summary = report.get_quality_summary()
        except Exception:  # nosec B110
            pass

    # Detect cycles for the report
    from aap_migration.analysis.dependency_graph import detect_cycles

    graph = {org: r.required_migrations_before for org, r in report.org_reports.items()}
    cycles = detect_cycles(graph)

    return {
        "analysis_date": report.analysis_date.isoformat() if report.analysis_date else None,
        "source_url": report.source_url,
        "total_organizations": report.total_organizations,
        "analyzed_organizations": report.analyzed_organizations,
        "independent_orgs": report.independent_orgs,
        "dependent_orgs": report.dependent_orgs,
        "migration_order": report.migration_order,
        "migration_phases": report.migration_phases,
        "circular_dependencies": [sorted(c) for c in cycles] if cycles else [],
        "organizations": org_data,
        "global_resources": {rtype: len(rlist) for rtype, rlist in report.global_resources.items()}
        if hasattr(report, "global_resources") and report.global_resources
        else {},
        "total_duplicates": report.total_duplicates if hasattr(report, "total_duplicates") else 0,
        "average_quality_score": report.average_quality_score
        if hasattr(report, "average_quality_score")
        else 100.0,
        "quality_summary": quality_summary,
    }
