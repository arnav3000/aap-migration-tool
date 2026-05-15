"""Dependency analysis endpoints."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from aap_migration.api.crypto import decrypt_token
from aap_migration.api.dependencies import get_db, get_job_service
from aap_migration.api.schemas import AnalysisRunRequest, JobStartResponse
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.job_service import Job

router = APIRouter()


def _serialize_report(report: Any) -> dict[str, Any]:
    """Serialize a GlobalDependencyReport to a dict matching the UI's AnalysisData shape."""
    orgs: dict[str, Any] = {}
    for name, org in report.org_reports.items():
        quality_data = None
        if org.quality_report:
            qr = org.quality_report
            naming = None
            if qr.naming_pattern:
                np = qr.naming_pattern
                naming = {
                    "dominant_pattern": np.dominant_pattern,
                    "consistency_score": np.consistency_score,
                    "total_resources": np.total_resources,
                    "case_style": np.case_style,
                    "prefixes": np.prefixes,
                    "separators": np.separators,
                    "violations": [
                        v if isinstance(v, dict) else {"detail": str(v)}
                        for v in (np.violations or [])
                    ],
                }
            quality_data = {
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
                    for d in (qr.duplicates or [])
                ],
                "naming_pattern": naming,
            }

        deps: dict[str, list[dict[str, Any]]] = {}
        for dep_org, dep_list in org.dependencies.items():
            deps[dep_org] = [
                {
                    "resource_type": d.resource_type,
                    "resource_id": d.resource_id,
                    "resource_name": d.resource_name,
                    "required_by": [
                        rb.get("name", str(rb)) if isinstance(rb, dict) else str(rb)
                        for rb in d.required_by
                    ],
                }
                for d in dep_list
            ]

        resource_counts: dict[str, int] = {}
        for rtype, rlist in org.resources.items():
            resource_counts[rtype] = len(rlist) if isinstance(rlist, list) else 0

        blocks: list[str] = []
        for other_name, other_org in report.org_reports.items():
            if name in other_org.required_migrations_before:
                blocks.append(other_name)

        orgs[name] = {
            "org_id": org.org_id,
            "resource_count": org.resource_count,
            "has_cross_org_deps": org.has_cross_org_deps,
            "can_migrate_standalone": org.can_migrate_standalone,
            "required_migrations_before": org.required_migrations_before,
            "blocks": blocks,
            "dependencies": deps,
            "quality": quality_data,
            "resources": resource_counts,
        }

    global_resources_counts: dict[str, int] = {}
    for rtype, rlist in report.global_resources.items():
        global_resources_counts[rtype] = len(rlist) if isinstance(rlist, list) else 0

    circular_deps: list[list[str]] = []
    try:
        from aap_migration.analysis.dependency_graph import topological_sort

        dep_map: dict[str, list[str]] = {}
        for name, org in report.org_reports.items():
            dep_map[name] = org.required_migrations_before
        topological_sort(dep_map)
    except ValueError:
        pass

    return {
        "analysis_date": report.analysis_date.isoformat(),
        "source_url": report.source_url,
        "total_organizations": report.total_organizations,
        "analyzed_organizations": report.analyzed_organizations,
        "independent_orgs": report.independent_orgs,
        "dependent_orgs": report.dependent_orgs,
        "migration_order": report.migration_order,
        "migration_phases": report.migration_phases,
        "organizations": orgs,
        "global_resources": global_resources_counts,
        "total_duplicates": report.total_duplicates,
        "average_quality_score": report.average_quality_score,
        "circular_dependencies": circular_deps,
    }


@router.post("/analysis/run", response_model=JobStartResponse)
async def run_analysis(body: AnalysisRunRequest, db: Session = Depends(get_db)) -> JobStartResponse:
    conn = ConnectionService.get(db, body.connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    svc = get_job_service()

    conn_url = conn.url
    conn_token = decrypt_token(conn.token)
    conn_verify = conn.verify_ssl
    conn_timeout = conn.timeout

    async def _do_analysis(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        from aap_migration.analysis.dependency_analyzer import CrossOrgDependencyAnalyzer
        from aap_migration.client.aap_source_client import AAPSourceClient
        from aap_migration.config import AAPInstanceConfig

        log(f"Starting dependency analysis for {conn_url}")

        config = AAPInstanceConfig(
            url=conn_url,
            token=conn_token,
            verify_ssl=conn_verify,
            timeout=conn_timeout,
        )
        client = AAPSourceClient(config)

        def progress_cb(current: object, total: object = None, msg: object = None) -> None:
            if msg:
                log(f"[{current}/{total}] {msg}")
            else:
                log(str(current))

        async with client:
            analyzer = CrossOrgDependencyAnalyzer(
                source_client=client,
                progress_callback=progress_cb,
            )
            report = await analyzer.analyze_all_organizations()

        log(f"Analysis complete: {report.total_organizations} organizations analyzed")
        log(
            f"Independent: {len(report.independent_orgs)}, "
            f"Dependent: {len(report.dependent_orgs)}"
        )

        serialized = _serialize_report(report)

        job._html_report = None  # type: ignore[attr-defined]
        try:
            from aap_migration.analysis.html_report import generate_html_report

            job._html_report = generate_html_report(report)  # type: ignore[attr-defined]
        except Exception as exc:
            log(f"Warning: HTML report generation failed: {exc}")

        return serialized

    job_id = svc.start_job(f"Analysis {conn_url}", "analysis", _do_analysis)
    return JobStartResponse(job_id=job_id)


@router.get("/analysis/{job_id}")
def get_analysis_result(job_id: str) -> dict[str, Any]:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    data = job.to_dict()
    if job.status == "completed" and job.result:
        data["data"] = job.result
    return data


@router.get("/analysis/{job_id}/export/json")
def export_analysis_json(job_id: str) -> Response:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed" or job.result is None:
        raise HTTPException(status_code=400, detail="Analysis not yet complete")

    content = json.dumps(job.result, indent=2, default=str)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="analysis-{job_id}.json"'},
    )


@router.get("/analysis/{job_id}/export/html")
def export_analysis_html(job_id: str) -> HTMLResponse:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed":
        raise HTTPException(status_code=400, detail="Analysis not yet complete")

    html = getattr(job, "_html_report", None)
    if html is None:
        raise HTTPException(status_code=400, detail="HTML report not available")

    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": f'attachment; filename="analysis-{job_id}.html"'},
    )
