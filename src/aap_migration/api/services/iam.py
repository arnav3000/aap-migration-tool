"""IAM background workers: audit, migrate, benchmark, report."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from aap_migration.api.jobs import TERMINAL_STATUSES, JobRecord
from aap_migration.api.services._core import (
    _resolve_iam_tls,
    benchmark_counts,
    iam_max_workers,
)


# -- IAM -------------------------------------------------------------------
def _stored_from_ctx(ctx: Any, side: str = "source") -> dict[str, Any]:
    """Derive a store-shaped connection dict from an already-built context.

    Single-resolution home: ``chained_ctx``/``setup_chained`` already
    resolved + SSRF-verified the pair; workers must not re-resolve via the
    store (which would double-resolve and risk observing a different pair
    across an admin set_active). Explicit ``verify_ssl``/``timeout`` params
    still win via :func:`_resolve_iam_tls`.
    """
    instance = getattr(getattr(ctx, "config", None), side, None)
    url = getattr(instance, "url", None)
    token = getattr(instance, "token", None)
    if url is None or token is None:
        raise ValueError(
            f"worker context carries no {side} connection; submit via the "
            "API so chained_ctx resolves and pins the pair"
        )
    return {
        "url": url,
        "token": token,
        "verify_ssl": getattr(instance, "verify_ssl", True),
        "timeout": getattr(instance, "timeout", 60),
    }


def _iam_analyser_kwargs(
    params: dict[str, Any], source: dict[str, Any], checkpoint_path: str
) -> dict[str, Any]:
    """Shared IAMAnalyser constructor kwargs (single home for the six)."""
    verify_ssl, timeout = _resolve_iam_tls(params, source)
    return {
        "source_url": source["url"],
        "source_token": source["token"],
        "verify_ssl": verify_ssl,
        "request_timeout": timeout,
        "max_workers": iam_max_workers(params),
        "scan_strategy": params.get("scan_strategy", "resource"),
        "checkpoint_path": checkpoint_path,
        "resume": bool(params.get("resume", False)),
    }


def _run_iam_audit(
    params: dict[str, Any],
    workdir: Path,
    source: dict[str, Any],
) -> dict[str, Any]:
    """Read-only IAM scan (audit lifecycle)."""
    from aap_migration.iam.analyser import IAMAnalyser
    from aap_migration.iam.report import write_iam_report

    out_dir = str(workdir / "iam_reports")
    checkpoint_path = str(Path(out_dir) / "iam_checkpoint.json")
    with IAMAnalyser(**_iam_analyser_kwargs(params, source, checkpoint_path)) as analyser:
        result = analyser.audit()
    json_path, html_path = write_iam_report(
        result, out_dir, json_filename="iam_audit.json", html_filename="iam_audit.html"
    )
    return {
        "message": "IAM audit complete",
        "json_report": str(Path(json_path).relative_to(workdir)),
        "html_report": str(Path(html_path).relative_to(workdir)),
        "stats": result.stats.to_dict() if hasattr(result.stats, "to_dict") else {},
    }


def _run_iam_migrate(
    params: dict[str, Any],
    workdir: Path,
    source: dict[str, Any],
    target: dict[str, Any] | None,
) -> dict[str, Any]:
    """IAM permission migration (migrate lifecycle)."""
    from aap_migration.iam.analyser import IAMAnalyser
    from aap_migration.iam.report import write_iam_report

    if target is None:
        raise ValueError("iam migrate requires a target AAP (pass target_id).")
    prefix = (
        "iam_teams_only"
        if params.get("skip_user_roles")
        else "iam_users_only"
        if params.get("users_only")
        else "iam_dry_run"
        if params.get("dry_run")
        else "iam_migration"
    )
    out_dir = str(workdir / "iam_reports")
    checkpoint_path = str(Path(out_dir) / "iam_checkpoint.json")
    with IAMAnalyser(
        **_iam_analyser_kwargs(params, source, checkpoint_path),
        target_url=target["url"],
        target_token=target["token"],
        state_db_path=str(workdir / "migration_state.db"),
    ) as analyser:
        result = analyser.migrate(
            dry_run=bool(params.get("dry_run", False)),
            skip_user_roles=bool(params.get("skip_user_roles", False)),
            users_only=bool(params.get("users_only", False)),
        )
    json_path, html_path = write_iam_report(
        result,
        out_dir,
        json_filename=f"{prefix}.json",
        html_filename=f"{prefix}.html",
    )
    return {
        "message": "IAM migration complete",
        "json_report": str(Path(json_path).relative_to(workdir)),
        "html_report": str(Path(html_path).relative_to(workdir)),
        "stats": result.stats.to_dict() if hasattr(result.stats, "to_dict") else {},
    }


def run_iam_audit(job: JobRecord) -> dict[str, Any]:
    """Read-only IAM scan (mirrors ``iam audit``)."""
    from aap_migration.api.services._core import chained_ctx

    # Resume onto failed/cancelled workdirs like migrate-resume (the
    # analyser continues from iam_checkpoint.json when resume=true).
    # Single pair resolution: reuse the already-built chained context.
    with chained_ctx(job, allow_statuses=TERMINAL_STATUSES, need="source") as (
        ctx,
        _config,
        workdir,
        params,
    ):
        pdict: dict[str, Any] = dict(params)
        source = _stored_from_ctx(ctx, "source")
        return _run_iam_audit(pdict, workdir, source)


def run_iam_migrate(job: JobRecord) -> dict[str, Any]:
    """Migrate IAM permissions (mirrors ``iam migrate``).

    Cancel is cooperative (best-effort): the flag is checked before the
    single analyser.migrate call, which cannot be preempted mid-call. A
    cancel landing mid-migrate runs to completion; the FIFO worker then
    reports cancelled with fence markers so resubmissions verify first.
    """
    from aap_migration.api.services._core import _cancel_requested, chained_ctx

    if _cancel_requested(job):
        return {"message": "IAM migration cancelled before start", "cancelled": True}
    # Single pair resolution: reuse the already-built chained context.
    with chained_ctx(job, allow_statuses=TERMINAL_STATUSES, need="both") as (
        ctx,
        _config,
        workdir,
        params,
    ):
        pdict: dict[str, Any] = dict(params)
        # Mutual-exclusion is enforced by the IamMigrateRequest schema; the check
        # below is defense-in-depth for direct worker invocation.
        if pdict.get("skip_user_roles") and pdict.get("users_only"):
            raise ValueError("--skip-user-roles and --users-only are mutually exclusive")
        if _cancel_requested(job):
            return {"message": "IAM migration cancelled", "cancelled": True}
        source = _stored_from_ctx(ctx, "source")
        target = _stored_from_ctx(ctx, "target")
        return _run_iam_migrate(pdict, workdir, source, target)


def run_iam_benchmark(job: JobRecord) -> dict[str, Any]:
    """Benchmark IAM API performance (mirrors ``iam benchmark``)."""
    from aap_migration.api.services._core import chained_ctx
    from aap_migration.iam.benchmark import run_benchmark

    # Single pair resolution: reuse the already-built chained context.
    with chained_ctx(job, need="source") as (ctx, _config, _workdir, params):
        pdict: dict[str, Any] = dict(params)
        source = _stored_from_ctx(ctx, "source")
        verify_ssl, _ = _resolve_iam_tls(pdict, source)
        sample_size = pdict.get("sample_size", 50)
        run_benchmark(
            source_url=source["url"],
            source_token=source["token"],
            verify_ssl=verify_ssl,
            sample_size=sample_size if isinstance(sample_size, int) else 50,
            worker_counts=benchmark_counts(pdict),
        )
        return {"message": "IAM benchmark complete"}


def run_iam_report(job: JobRecord) -> dict[str, Any]:
    """Regenerate IAM HTML report from JSON (mirrors ``iam report``).

    ``json_path`` must resolve under the job base dir; free absolute server
    paths are rejected. Prefer ``job_id`` chaining, which resolves the
    referenced job's ``json_report`` result (job-dir bounded).
    """
    from aap_migration.api.jobs import get_job_manager
    from aap_migration.api.security import confine_path
    from aap_migration.api.services._core import workdir_ctx
    from aap_migration.iam.report import (
        generate_iam_html_report,
        load_audit_result_from_json,
    )

    # Connectionless worker (need="none", no snapshot pins): workdir +
    # logging lifecycle only, no pair resolution.
    with workdir_ctx(job) as (workdir, params):
        pdict: dict[str, Any] = dict(params)
        json_path = pdict.get("json_path")
        ref_dir = None
        ref_id = pdict.get("job_id")
        if not json_path and ref_id:
            ref = get_job_manager().get_internal(ref_id)
            ref_dir = Path(ref["job_dir"]).resolve()
            result_payload = ref.get("result") or {}
            candidate = result_payload.get("json_report")
            if candidate:
                cand_path = Path(candidate)
                if not cand_path.is_absolute() and ref_dir is not None:
                    # Chained results carry workdir-relative paths: resolve them
                    # against the referenced job's directory first.
                    under_ref = ref_dir / cand_path
                    if under_ref.is_file():
                        json_path = str(under_ref)
                    else:
                        json_path = candidate
                else:
                    json_path = candidate
        if not json_path:
            raise ValueError("json_path (or job_id with a JSON report) is required")
        if ref_dir is not None and not Path(json_path).is_absolute():
            base_dir = str(ref_dir)
        else:
            base_dir = get_job_manager().base_dir
        try:
            confined = confine_path(json_path, base_dir, label="json_path")
        except ValueError as exc:
            raise ValueError("json_path must stay under the job file tree") from exc
        if not confined.is_file():
            raise ValueError(f"json report not found: {confined.name}")
        result = load_audit_result_from_json(str(confined))
        out_dir = workdir / "iam_reports"
        out_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        html_path = out_dir / (confined.stem + ".html")
        fd = os.open(html_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(generate_iam_html_report(result))
        try:
            os.chmod(html_path, 0o600)
        except OSError:
            pass
        return {
            "message": "IAM HTML report generated",
            "html_report": str(html_path.relative_to(workdir)),
        }
