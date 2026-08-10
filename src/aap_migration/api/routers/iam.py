"""IAM analysis, benchmark, and report endpoints."""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import Callable
from contextlib import redirect_stdout
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, get_job_service
from aap_migration.api.schemas import (
    IAMAnalyseRequest,
    IAMBenchmarkRequest,
    IAMReportRequest,
    JobStartResponse,
)
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.job_service import Job, JobStatus

router = APIRouter()


def _connection_token_and_ssl(conn: Any, verify_ssl: bool | None) -> tuple[str, bool]:
    from aap_migration.api.crypto import decrypt_token

    token = decrypt_token(conn.token) if conn.token else ""
    if not token:
        raise HTTPException(status_code=400, detail="Connection has no authentication token")
    effective_ssl = conn.verify_ssl if verify_ssl is None else verify_ssl
    return token, effective_ssl


@router.post("/iam/analyse", response_model=JobStartResponse)
async def run_iam_analyse(
    body: IAMAnalyseRequest, db: Session = Depends(get_db)
) -> JobStartResponse:
    conn = ConnectionService.get(db, body.connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    token, verify_ssl = _connection_token_and_ssl(conn, body.verify_ssl)
    inst_config = ConnectionService.build_instance_config(conn)
    cp_dir = body.checkpoint_dir or body.output_dir
    checkpoint_path = os.path.join(cp_dir, "iam_checkpoint.json")
    output_dir = body.output_dir
    svc = get_job_service()

    async def _do_analyse(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        from aap_migration.iam.analyser import IAMAnalyser
        from aap_migration.iam.report import generate_iam_html_report, write_iam_report

        def _progress(msg: str) -> None:
            log(msg)

        def _run_audit() -> Any:
            with IAMAnalyser(
                source_url=inst_config.url,
                source_token=token,
                verify_ssl=verify_ssl,
                request_timeout=body.timeout,
                max_workers=body.workers,
                scan_strategy=body.scan_strategy,
                checkpoint_path=checkpoint_path,
                resume=body.resume,
                progress_callback=_progress,
            ) as analyser:
                return analyser.audit()

        log(f"Starting IAM audit for {inst_config.url}")
        result = await asyncio.to_thread(_run_audit)
        json_path, html_path = await asyncio.to_thread(
            write_iam_report,
            result,
            output_dir,
            "iam_audit.json",
            "iam_audit.html",
        )
        job._html_report = await asyncio.to_thread(generate_iam_html_report, result)

        stats = result.stats
        log(
            f"IAM audit complete: {stats.resources_scanned} resources, "
            f"{stats.permissions_found} permissions"
        )
        return {
            "json_path": json_path,
            "html_path": html_path,
            "output_dir": output_dir,
            "stats": {
                "resources_scanned": stats.resources_scanned,
                "permissions_found": stats.permissions_found,
                "permissions_deduplicated": stats.permissions_deduplicated,
                "team_memberships_found": stats.team_memberships_found,
                "system_roles_found": stats.system_roles_found,
                "cross_org_shares": stats.cross_org_shares,
            },
        }

    job_id = svc.start_job(f"IAM Analyse {conn.name}", "iam-analyse", _do_analyse)
    return JobStartResponse(job_id=job_id)


@router.post("/iam/benchmark", response_model=JobStartResponse)
async def run_iam_benchmark(
    body: IAMBenchmarkRequest, db: Session = Depends(get_db)
) -> JobStartResponse:
    conn = ConnectionService.get(db, body.connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    token, verify_ssl = _connection_token_and_ssl(conn, body.verify_ssl)
    inst_config = ConnectionService.build_instance_config(conn)
    worker_counts = body.workers if body.workers else [1, 10, 20]
    svc = get_job_service()

    async def _do_benchmark(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        from aap_migration.iam.benchmark import run_benchmark

        buffer = io.StringIO()

        def _run() -> None:
            with redirect_stdout(buffer):
                run_benchmark(
                    source_url=inst_config.url,
                    source_token=token,
                    verify_ssl=verify_ssl,
                    sample_size=body.sample_size,
                    worker_counts=worker_counts,
                )

        log(f"Starting IAM benchmark for {inst_config.url}")
        await asyncio.to_thread(_run)
        output = buffer.getvalue()
        for line in output.splitlines():
            log(line)
        return {
            "source_url": inst_config.url,
            "sample_size": body.sample_size,
            "worker_counts": worker_counts,
            "benchmark_output": output,
        }

    job_id = svc.start_job(f"IAM Benchmark {conn.name}", "iam-benchmark", _do_benchmark)
    return JobStartResponse(job_id=job_id)


@router.post("/iam/report", response_model=JobStartResponse)
async def run_iam_report(body: IAMReportRequest) -> JobStartResponse:
    svc = get_job_service()
    json_path = body.json_path

    if body.job_id:
        prior = svc.get_job(body.job_id)
        if prior is None:
            raise HTTPException(status_code=404, detail="Prior job not found")
        if not prior.result or not prior.result.get("json_path"):
            raise HTTPException(status_code=400, detail="Prior job has no JSON report path")
        json_path = prior.result["json_path"]

    assert json_path is not None
    if not os.path.isfile(json_path):
        raise HTTPException(status_code=400, detail=f"JSON file not found: {json_path}")

    output_dir = body.output_dir
    resolved_json = json_path

    async def _do_report(job: Job, log: Callable[[str], None]) -> dict[str, Any]:
        from aap_migration.iam.report import (
            generate_iam_html_report,
            load_audit_result_from_json,
        )

        def _run() -> tuple[str, str]:
            result = load_audit_result_from_json(resolved_json)
            target_dir = output_dir or os.path.dirname(os.path.abspath(resolved_json))
            html_content = generate_iam_html_report(result)
            html_filename = os.path.splitext(os.path.basename(resolved_json))[0] + ".html"
            html_path = os.path.join(target_dir, html_filename)
            os.makedirs(target_dir, mode=0o700, exist_ok=True)
            fd = os.open(html_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(html_content)
            return html_path, html_content

        log(f"Regenerating IAM HTML report from {resolved_json}")
        html_path, html_content = await asyncio.to_thread(_run)
        job._html_report = html_content
        log(f"HTML report generated: {html_path}")
        return {"json_path": resolved_json, "html_path": html_path}

    job_id = svc.start_job("IAM Report", "iam-report", _do_report)
    return JobStartResponse(job_id=job_id)


@router.get("/iam/{job_id}")
def get_iam_result(job_id: str) -> dict[str, Any]:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@router.get("/iam/{job_id}/export/html")
def export_iam_html(job_id: str) -> HTMLResponse:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_ERRORS):
        raise HTTPException(status_code=400, detail="IAM job not yet complete")

    html = getattr(job, "_html_report", None)
    if html is None and job.result:
        html_path = job.result.get("html_path")
        if html_path and os.path.isfile(html_path):
            with open(html_path) as fh:
                html = fh.read()
    if html is None:
        raise HTTPException(status_code=400, detail="HTML report not available")

    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": f'attachment; filename="iam-{job_id}.html"'},
    )


@router.get("/iam/{job_id}/export/json")
def export_iam_json(job_id: str) -> Response:
    svc = get_job_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_ERRORS):
        raise HTTPException(status_code=400, detail="IAM job not yet complete")

    json_path = job.result.get("json_path") if job.result else None
    if not json_path or not os.path.isfile(json_path):
        raise HTTPException(status_code=400, detail="JSON report not available")

    with open(json_path) as fh:
        content = fh.read()
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="iam-{job_id}.json"'},
    )
