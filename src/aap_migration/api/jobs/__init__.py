"""Background job manager for long-running API operations (package).

Split from the former single ``jobs.py`` module along section seams; this
package re-exports every public name so ``from aap_migration.api.jobs
import ...`` keeps working unchanged:

- :mod:`aap_migration.api.jobs._config` -- env bounds + startup health
- :mod:`aap_migration.api.jobs._records` -- record shapes + result envelope
- :mod:`aap_migration.api.jobs._scrub` -- secret redaction
- :mod:`aap_migration.api.jobs._console` -- capture + bounded log helpers
- :mod:`aap_migration.api.jobs._fences` -- orphan-fence tracking (see FenceTracker)
- :mod:`aap_migration.api.jobs._index` -- restart-durable terminal index
- :mod:`aap_migration.api.jobs._retention` -- MAX_JOBS eviction + dir removal
- :mod:`aap_migration.api.jobs.manager` -- FIFO :class:`JobManager` (queue/transition facade)
- :mod:`aap_migration.api.jobs._worker` -- worker loop + fence/park orchestration mixin

Public records never expose server-local filesystem paths; the internal
record keeps ``job_dir`` for workers only. Error strings returned to callers
are generic ids plus a short console tail -- tracebacks are logged
server-side, never returned.
"""

from __future__ import annotations

import threading

from aap_migration.api.jobs._config import (
    CONSOLE_MAX_BYTES,
    CONSOLE_TAIL_BYTES,
    JOB_TIMEOUT_SECS,
    MAX_JOBS,
    MAX_ORPHANS,
    MAX_QUEUE_DEPTH,
    ORPHAN_QUARANTINE_MAX,
    ORPHAN_SWEEP_AGE_SECS,
    clear_startup_degraded_if_recovered,
    set_startup_degraded,
    startup_degraded_reason,
)
from aap_migration.api.jobs._console import (
    _bounded_output,
    _console_tail,
    _persist_console,
    _ThreadLocalProxy,
    read_console_tail,
)
from aap_migration.api.jobs._records import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    ConflictError,
    InternalStatusError,
    JobRecord,
    QueueFullError,
    ServerShuttingDownError,
    StorageUnhealthyError,
    UnknownJobError,
    WorkdirGoneError,
    _normalize_result,
    _utcnow,
    public_job_params,
)
from aap_migration.api.jobs._scrub import _scrub_output as _scrub_output  # noqa: F401
from aap_migration.api.jobs.manager import JobManager

__all__ = [
    "CONSOLE_MAX_BYTES",
    "CONSOLE_TAIL_BYTES",
    "JOB_TIMEOUT_SECS",
    "MAX_JOBS",
    "MAX_ORPHANS",
    "MAX_QUEUE_DEPTH",
    "ORPHAN_QUARANTINE_MAX",
    "ORPHAN_SWEEP_AGE_SECS",
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "ConflictError",
    "InternalStatusError",
    "UnknownJobError",
    "WorkdirGoneError",
    "JobManager",
    "JobRecord",
    "QueueFullError",
    "ServerShuttingDownError",
    "StorageUnhealthyError",
    "public_job_params",
    "_ThreadLocalProxy",
    "_bounded_output",
    "_console_tail",
    "_normalize_result",
    "_persist_console",
    "_scrub_output",
    "_utcnow",
    "clear_startup_degraded_if_recovered",
    "get_job_manager",
    "manager_or_none",
    "read_console_tail",
    "reset_job_manager",
    "set_startup_degraded",
    "startup_degraded_reason",
]

_manager: JobManager | None = None
_manager_lock = threading.Lock()


def get_job_manager() -> JobManager:
    """Return the process-wide JobManager singleton."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = JobManager()
        else:
            _manager.ensure_worker()
        return _manager


def reset_job_manager(base_dir: str | None = None) -> JobManager:
    """Reset the singleton (used by tests)."""
    global _manager
    with _manager_lock:
        _manager = JobManager(base_dir=base_dir)
        return _manager


def manager_or_none() -> JobManager | None:
    """Return the singleton without creating or probing it (readiness paths).

    Returns None when no manager exists yet, so health/lifespan checks can
    report "unknown" instead of constructing (or restarting) a worker as a
    read side effect.
    """
    with _manager_lock:
        return _manager
