"""Read-model mixin for JobManager (split from manager.py).

Single home for locked status scans, guards, and pressure snapshots so
``manager.py`` stays a thin admission/transition facade. Operates on
``self._lock``/``self._jobs``/``self._fences`` owned by JobManager; no
independent state here.
"""

from __future__ import annotations

import threading
from typing import Any, cast

_ACTIVE_JOBS_MESSAGE = (
    "Server-default state reset requires zero running jobs; "
    "{active} job(s) still active. Wait or cancel them first."
)


def _active_count(jobs: Any) -> int:
    """Count queued + running jobs (single home for the status scan)."""
    from aap_migration.api.jobs._records import ACTIVE_STATUSES

    return sum(1 for j in jobs if j.get("status") in ACTIVE_STATUSES)


def _check_terminal(job: Any, job_id: str) -> Any:
    """Return a copy of *job* when terminal; fail loudly otherwise."""
    from aap_migration.api.jobs._records import ACTIVE_STATUSES, ConflictError

    status = job.get("status")
    if status in ACTIVE_STATUSES:
        raise ConflictError(
            f"Job '{job_id}' is {status}; wait for it to reach "
            "a terminal state before resetting or importing its state DB."
        )
    return cast(Any, dict(job))


class JobReadMixin:
    """Locked read model + lifecycle guards (mixin for JobManager)."""

    # Attributes provided by JobManager; declared for type-checkers.
    _lock: threading.RLock
    _jobs: dict[str, Any]
    _fences: Any

    def _unknown_job_message(self, job_id: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def get(self, job_id: str) -> Any:
        """Return a job record. Raises UnknownJobError if unknown."""
        from aap_migration.api.jobs._records import UnknownJobError

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise UnknownJobError(self._unknown_job_message(job_id))
            return self._public(job)  # type: ignore[attr-defined]

    def get_internal(self, job_id: str) -> Any:
        """Return the internal record including ``job_dir`` (workers only)."""
        from aap_migration.api.jobs._records import UnknownJobError

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise UnknownJobError(self._unknown_job_message(job_id))
            return cast(Any, dict(job))

    def list_jobs(self, status: str | None = None, limit: int = 100) -> list[Any]:
        """List jobs, newest first, optionally filtered by status."""
        from aap_migration.api.jobs import _config as _job_config

        limit = max(1, min(int(limit), max(1000, _job_config.MAX_JOBS)))
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)
        if status:
            jobs = [j for j in jobs if j["status"] == status]
        return [self._public(j) for j in jobs[:limit]]  # type: ignore[attr-defined]

    def count(self, status: str | None = None) -> int:
        """Return the true pre-page job count for an optional status filter."""
        with self._lock:
            if status:
                return sum(1 for j in self._jobs.values() if j["status"] == status)
            return len(self._jobs)

    def non_terminal_params(self) -> list[dict[str, Any]]:
        """Return internal params of every non-terminal job (lifecycle guards)."""
        from aap_migration.api.jobs._records import TERMINAL_STATUSES

        with self._lock:
            return [
                dict(job.get("params") or {})
                for job in self._jobs.values()
                if job.get("status") not in TERMINAL_STATUSES
            ]

    def worker_alive(self) -> bool:
        """Return True when the FIFO worker thread is alive."""
        worker = getattr(self, "_worker", None)
        return bool(worker is not None and worker.is_alive())

    def queue_depth(self) -> int:
        """Number of queued (not yet running) jobs."""
        with self._lock:
            return sum(1 for j in self._jobs.values() if j["status"] == "queued")

    def has_active_jobs(self) -> bool:
        """True when any job is queued or running (locked status scan)."""
        with self._lock:
            return _active_count(self._jobs.values()) > 0

    def active_job_count(self) -> int:
        """Number of queued + running jobs (locked status scan)."""
        with self._lock:
            return _active_count(self._jobs.values())

    def assert_no_active_jobs(self) -> None:
        """Raise ConflictError when any job is queued/running."""
        from aap_migration.api.jobs._records import ConflictError

        with self._lock:
            active = _active_count(self._jobs.values())
        if active:
            raise ConflictError(_ACTIVE_JOBS_MESSAGE.format(active=active))

    def assert_no_active_jobs_locked(self) -> None:
        """Same as assert_no_active_jobs but caller holds state_write_lock."""
        from aap_migration.api.jobs._records import ConflictError

        active = _active_count(self._jobs.values())
        if active:
            raise ConflictError(_ACTIVE_JOBS_MESSAGE.format(active=active))

    def assert_job_terminal(self, job_id: str) -> Any:
        """Return a copy of *job_id* when terminal; fail loudly otherwise."""
        from aap_migration.api.jobs._records import UnknownJobError

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise UnknownJobError(self._unknown_job_message(job_id))
            return _check_terminal(job, job_id)

    def assert_job_terminal_locked(self, job_id: str) -> Any:
        """Same as assert_job_terminal but caller holds state_write_lock."""
        from aap_migration.api.jobs._records import UnknownJobError

        job = self._jobs.get(job_id)
        if job is None:
            raise UnknownJobError(self._unknown_job_message(job_id))
        return _check_terminal(job, job_id)

    def state_write_lock(self) -> threading.RLock:
        """Critical section for destructive state writes (reset/import)."""
        return self._lock

    def has_live_fences(self) -> bool:
        """True when timed-out orphans are still applying writes."""
        try:
            snap = self._fences.snapshot()
            return bool(snap.get("orphans")) or bool(snap.get("fenced_dirs"))
        except Exception:
            return False

    def assert_no_live_fences(self) -> None:
        """Raise ConflictError while orphan fences are live (state guards)."""
        from aap_migration.api.jobs._records import ConflictError

        if self.has_live_fences():
            raise ConflictError(
                "Timed-out job still draining (fenced); wait for orphan drain "
                "before resetting or importing state."
            )

    def pressure(self) -> dict[str, int | None]:
        """Queue + orphan pressure snapshot (never raises)."""
        try:
            with self._lock:
                queued = sum(1 for j in self._jobs.values() if j["status"] == "queued")
                running = sum(1 for j in self._jobs.values() if j["status"] == "running")
            snapshot = self._fences.snapshot()
            return {
                "queued": queued,
                "running": running,
                "orphans": snapshot["orphans"],
                "fenced_dirs": snapshot["fenced_dirs"],
            }
        except Exception:
            return {"queued": None, "running": None, "orphans": None, "fenced_dirs": None}
