"""System endpoints: health, readiness, version, resource catalog."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from aap_migration import __version__
from aap_migration.api.schemas import (
    HealthOut,
    ReadyOut,
    ResourceDetailOut,
    ResourcesOut,
    VersionOut,
)
from aap_migration.api.security import require_api_key

router = APIRouter(tags=["system"])
log = logging.getLogger("aap_migration.api.routers.system")


def _worker_state() -> tuple[str, int | None]:
    """Inspect the FIFO worker without restarting it (no probe side effects).

    Unknown depths are None (never a -1 sentinel): numeric consumers must
    not fold them into arithmetic during startup when the manager is absent.
    """
    try:
        from aap_migration.api.jobs import manager_or_none

        manager = manager_or_none()
        if manager is None:
            # No manager yet means no jobs submitted since boot: the
            # process is serving, so report healthy instead of 503.
            # The first submit lazily creates the manager.
            return "alive", 0
        # Public API (locked queue_depth) instead of lock-free _jobs iteration.
        return ("alive" if manager.worker_alive() else "dead"), manager.queue_depth()
    except Exception:
        return "unknown", None


def _orphan_state() -> dict[str, int | None]:
    """Orphan/fence pressure for load-shedding visibility (never raises).

    Unknown values are None (never -1 sentinels).
    """
    try:
        from aap_migration.api.jobs import manager_or_none

        manager = manager_or_none()
        if manager is None:
            return {"orphans": None, "fenced_dirs": None}
        snapshot = manager.pressure()
        return {"orphans": snapshot["orphans"], "fenced_dirs": snapshot["fenced_dirs"]}
    except Exception:
        return {"orphans": None, "fenced_dirs": None}


def _sanitize_degraded(degraded: str | None) -> str | None:
    """Return a path-free degraded marker for serving (CWE-209).

    The latch writers store generic markers, but a path-bearing value set
    by an older process (or a future writer regression) must never reach
    pollers verbatim: preserve only the component prefix.
    """
    if not degraded:
        return None
    parts: list[str] = []
    for chunk in str(degraded).split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "job-dir" in chunk:
            parts.append("job-dir: storage-unhealthy")
        elif "api-db" in chunk:
            parts.append("api-db: storage-unhealthy")
        else:
            parts.append("storage-unhealthy")
    return "; ".join(parts) if parts else "storage-unhealthy"


@router.get("/health", response_model=HealthOut)
def health() -> JSONResponse:
    """Liveness probe (includes FIFO worker state for orchestration).

    Keyless by design (see app.create_app): orchestrator liveness probes
    rarely carry API keys, so gating health behind auth turns every probe
    into a 401 and restarts a healthy server.
    """
    worker, depth = _worker_state()
    degraded = None
    try:
        from aap_migration.api.jobs import startup_degraded_reason

        degraded = _sanitize_degraded(startup_degraded_reason())
    except Exception:
        degraded = None
    status = "ok" if worker == "alive" and not degraded else "degraded"
    code = 200 if worker == "alive" and not degraded else 503
    content: dict = {
        "status": status,
        "service": "aap-bridge-api",
        "version": __version__,
        "worker": worker,
        "queue_depth": depth,
        **_orphan_state(),
    }
    if degraded:
        content["startup_degraded"] = degraded
    return JSONResponse(status_code=code, content=content)


def probe_dir_writable(directory: str) -> None:
    """Real write probe: mkdir + create + fsync + unlink (raises on failure).

    Shared home for the lifespan startup check (app) and the readiness
    probe (here) so the two definitions of writable cannot drift.
    """
    import os
    from pathlib import Path

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    probe = target / ".writability_probe"
    probe.write_text("ok")
    try:
        with open(probe, "rb") as fh:
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
    finally:
        try:
            probe.unlink(missing_ok=True)
        except Exception:
            pass


@router.get("/ready", response_model=ReadyOut)
def ready() -> JSONResponse:
    """Readiness probe: worker alive plus DB and job-dir writability.

    Keyless by design (see app.create_app): orchestrator readiness probes
    rarely carry API keys.
    """
    import os
    import sqlite3
    from pathlib import Path

    worker, depth = _worker_state()
    checks: dict[str, str] = {"worker": worker}
    ok = worker == "alive"
    if not ok:
        checks["worker"] = f"not-ready: {worker}"
    try:
        # Read-only probe: no init_api_db() DDL side effect here.
        from aap_migration.api.models import api_db_path

        db_path = api_db_path()
        if "://" in db_path and not db_path.startswith("sqlite"):
            checks["database"] = "writable (non-sqlite; skipped)"
        else:
            fs_path = db_path
            for scheme in ("sqlite:///", "sqlite://"):
                if fs_path.startswith(scheme):
                    fs_path = fs_path[len(scheme) :]
                    break
            if not fs_path or not os.path.exists(fs_path):
                log.warning("readiness database probe failed: missing file")
                checks["database"] = "unwritable: database"
                ok = False
            else:
                parent = str(Path(os.path.abspath(fs_path)).parent)
                probe_dir_writable(parent)
                conn = sqlite3.connect(f"file:{fs_path}?mode=ro", uri=True, timeout=5)
                try:
                    conn.execute("SELECT 1")
                finally:
                    conn.close()
                checks["database"] = "writable"
    except Exception as exc:
        log.warning("readiness database probe failed: %s", exc)
        checks["database"] = "unwritable: database"
        ok = False
    try:
        job_dir = os.environ.get("AAP_BRIDGE_JOB_DIR") or "./api_jobs"
        probe_dir_writable(job_dir)
        checks["job_dir"] = "writable"
    except Exception as exc:
        log.warning("readiness job-dir probe failed: %s", exc)
        checks["job_dir"] = "unwritable: job_dir"
        ok = False
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "ready": ok,
            "checks": checks,
            "queue_depth": depth,
            **_orphan_state(),
        },
    )


@router.get("/version", response_model=VersionOut, dependencies=[Depends(require_api_key)])
def version() -> dict:
    """API + CLI version info (mirrors ``--version``)."""
    return {"api": __version__, "prog_name": "aap-bridge"}


@router.get(
    "/resources",
    response_model=ResourcesOut,
    dependencies=[Depends(require_api_key)],
)
def list_resources() -> dict:
    """Full resource catalog (mirrors ``resources.py`` registry)."""
    from aap_migration.resources import (
        ALL_RESOURCE_TYPES,
        FULLY_SUPPORTED_TYPES,
        RESOURCE_REGISTRY,
        get_cleanup_order,
        get_migration_order,
    )

    return {
        "all": list(ALL_RESOURCE_TYPES),
        "fully_supported": list(FULLY_SUPPORTED_TYPES),
        "migration_order": list(get_migration_order()),
        "cleanup_order": list(get_cleanup_order()),
        "resources": {
            name: {
                "endpoint": info.endpoint,
                "description": info.description,
                "migration_order": info.migration_order,
                "cleanup_order": info.cleanup_order,
                "has_exporter": info.has_exporter,
                "has_importer": info.has_importer,
                "has_transformer": info.has_transformer,
                "batch_size": info.batch_size,
            }
            for name, info in RESOURCE_REGISTRY.items()
        },
    }


@router.get(
    "/resources/{resource_type}",
    response_model=ResourceDetailOut,
    dependencies=[Depends(require_api_key)],
)
def get_resource(resource_type: str) -> dict:
    """Details for a single resource type."""
    from aap_migration.resources import get_info, normalize_resource_type

    try:
        normalized = normalize_resource_type(resource_type)
        info = get_info(normalized)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"Unknown resource type '{resource_type}'"
        ) from exc
    return {
        "name": normalized,
        "endpoint": info.endpoint,
        "description": info.description,
        "migration_order": info.migration_order,
        "cleanup_order": info.cleanup_order,
        "has_exporter": info.has_exporter,
        "has_importer": info.has_importer,
        "has_transformer": info.has_transformer,
        "batch_size": info.batch_size,
    }
