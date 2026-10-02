"""Environment bounds and startup storage health for API background jobs.

Single home for the ``AAP_BRIDGE_*`` tunables and the startup-degraded
flag so queueing, retention, and console-format concerns do not all live
in the FIFO manager module.
"""

from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("aap_migration.api.jobs")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        log.warning("invalid int env %s=%r; using default %d", name, os.environ.get(name), default)
        return default


def _env_float(name: str, default: float) -> float:
    import math

    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        log.warning(
            "invalid float env %s=%r; using default %s", name, os.environ.get(name), default
        )
        return default
    if not math.isfinite(value):
        log.warning(
            "non-finite float env %s=%r; using default %s", name, os.environ.get(name), default
        )
        return default
    return value


def _clamp_int(name: str, value: int, minimum: int, maximum: int, default: int) -> int:
    if value < minimum or value > maximum:
        log.warning(
            "env %s=%d out of range [%d, %d]; clamping (default %d)",
            name,
            value,
            minimum,
            maximum,
            default,
        )
        return min(max(value, minimum), maximum)
    return value


def _clamp_float(name: str, value: float, minimum: float, maximum: float, default: float) -> float:
    if value < minimum or value > maximum:
        log.warning(
            "env %s=%s out of range [%s, %s]; clamping (default %s)",
            name,
            value,
            minimum,
            maximum,
            default,
        )
        return min(max(value, minimum), maximum)
    return value


MAX_JOBS = _clamp_int("AAP_BRIDGE_MAX_JOBS", _env_int("AAP_BRIDGE_MAX_JOBS", 1000), 1, 100000, 1000)
MAX_QUEUE_DEPTH = _clamp_int(
    "AAP_BRIDGE_MAX_QUEUE", _env_int("AAP_BRIDGE_MAX_QUEUE", 100), 1, 10000, 100
)
JOB_TIMEOUT_SECS = _clamp_float(
    "AAP_BRIDGE_JOB_TIMEOUT",
    _env_float("AAP_BRIDGE_JOB_TIMEOUT", 3600.0),
    60.0,
    7200.0,
    3600.0,
)
CONSOLE_MAX_BYTES = 1 << 20  # 1 MiB append cap per console.log
CONSOLE_TAIL_BYTES = 256 << 10  # 256 KiB served to console polling
# Bound timed-out orphan tracking so one slow target cannot wedge the single
# FIFO worker forever: new submissions shed load (429) instead of queueing
# behind fenced directories, and the worker fails fenced jobs fast.
MAX_ORPHANS = _clamp_int(
    "AAP_BRIDGE_MAX_ORPHANS", _env_int("AAP_BRIDGE_MAX_ORPHANS", 50), 1, 1000, 50
)


# Startup storage health: lifespan sets a degraded reason when the API DB
# or job-dir writability probes fail. Submissions fail fast with 503 until
# the probes pass, instead of accepting 202s for jobs guaranteed to fail
# on persistence. Tests/local dev keep warn-only via
# AAP_BRIDGE_ALLOW_DEGRADED_STARTUP=1 (explicit opt-in).
_startup_degraded: str | None = None
_startup_lock = threading.Lock()


def set_startup_degraded(reason: str | None) -> None:
    """Record (or clear) the startup storage-degraded flag."""
    global _startup_degraded
    with _startup_lock:
        _startup_degraded = reason


def startup_degraded_reason() -> str | None:
    """Return the startup-degraded reason, or None when storage is healthy."""
    with _startup_lock:
        return _startup_degraded


def clear_startup_degraded_if_recovered() -> bool:
    """Re-probe startup storage and clear the degraded flag when healthy.

    The lifespan probe in ``api.app`` latches ``_startup_degraded`` when the
    API DB or job-dir writability checks fail; without a re-probe every
    later :meth:`JobManager.submit` keeps returning 503 even after the
    underlying storage recovers (e.g. a transient mount delay). This helper
    re-runs the same two probes (API DB init + job-dir writability) and
    clears the flag when both pass so submissions proceed instead of 503.

    Respects ``AAP_BRIDGE_ALLOW_DEGRADED_STARTUP=1`` (explicit dev opt-in):
    when set, the flag is cleared and True is returned regardless of probe
    outcome, preserving the historical warn-only behavior for tests/local
    dev. Returns True when healthy (flag cleared), False when still
    degraded (flag updated with the fresh reason).
    """
    import pathlib

    if os.environ.get("AAP_BRIDGE_ALLOW_DEGRADED_STARTUP", "") == "1":
        set_startup_degraded(None)
        return True
    degraded: list[str] = []
    try:
        from aap_migration.api.models import init_api_db

        db_path = init_api_db()
        pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        degraded.append(f"api-db: {exc}")
    job_dir = os.environ.get("AAP_BRIDGE_JOB_DIR") or "./api_jobs"
    try:
        pathlib.Path(job_dir).mkdir(parents=True, exist_ok=True)
        probe = pathlib.Path(job_dir) / ".writability_probe"
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
    except Exception as exc:
        degraded.append(f"job-dir: {exc}")
    if degraded:
        set_startup_degraded("; ".join(degraded))
        return False
    set_startup_degraded(None)
    log.info("startup storage re-probe passed; degraded flag cleared")
    return True
