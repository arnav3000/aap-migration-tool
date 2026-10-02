"""Thread-safe FIFO background job manager.

Jobs run sequentially in a single FIFO worker thread. Sequential execution is
deliberate: migration phases rely on per-job config files and chained working
directories, and concurrent migrations against the same AAP pair would
conflict.

Lifecycle: ``queued -> running -> succeeded | failed | cancelled``.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import queue
import threading
import uuid
from collections.abc import Callable
from io import StringIO
from typing import Any, cast

import click

from aap_migration.api.jobs import _config as _job_config
from aap_migration.api.jobs._config import _env_float, startup_degraded_reason
from aap_migration.api.jobs._console import (
    _bounded_output,
    _console_tail,
    _persist_console,
    _stderr_proxy,
    _stdout_proxy,
)
from aap_migration.api.jobs._fences import FenceTracker, daemon_thread_pool
from aap_migration.api.jobs._index import RestartIndex
from aap_migration.api.jobs._reads import JobReadMixin
from aap_migration.api.jobs._records import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    ConflictError,
    JobRecord,
    QueueFullError,
    UnknownJobError,
    _normalize_result,
    _utcnow,
)
from aap_migration.api.store import SNAPSHOT_FP

log = logging.getLogger("aap_migration.api.jobs")


class JobManager(JobReadMixin):
    """Thread-safe FIFO background job manager.

    In-memory state (queued/running records, orphan fences) is not durable
    across restarts: a restart drops all records and fences, and queued work
    must be resubmitted. To keep restarts actionable instead of bare 404s,
    every terminal transition appends a summary line to
    ``<base_dir>/.job_index.jsonl`` (bounded); unknown ids found there
    report "submitted before the last restart; resubmit to rerun".
    Orphaned job directories from a previous run are counted and logged at
    startup for observability (never auto-deleted: they may hold audit
    artifacts). Operators should treat restart as loss of live queue state;
    completed job artifacts on disk survive unless evicted.

    Single-lane tradeoffs: DNS re-verification is bounded (10s per job) so
    one poison hostname fails one job fast instead of wedging the worker,
    and MAX_QUEUE_DEPTH bounds how many such jobs can stack up; a flood of
    unresolvable-host jobs still head-of-line blocks legitimate work behind
    it, by design of the single FIFO lane.
    """

    def __init__(self, base_dir: str | None = None) -> None:
        raw = base_dir or os.environ.get("AAP_BRIDGE_JOB_DIR") or "./api_jobs"
        self.base_dir = os.path.abspath(raw)
        os.makedirs(self.base_dir, exist_ok=True)
        self._index = RestartIndex(self.base_dir, _job_config.MAX_JOBS)
        self._index.log_stale_dirs()
        self.job_timeout = _job_config.JOB_TIMEOUT_SECS
        self._jobs: dict[str, JobRecord] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._funcs: dict[str, Callable[[JobRecord], dict[str, Any]]] = {}
        # RLock so state-write critical sections can re-check guards while
        # holding the same lock routers use for submit/status scans.
        self._lock = threading.RLock()
        self._worker_lock = threading.Lock()
        # Timed-out attempts the pool thread cannot preempt: the worker must
        # not start overlapping work on a fenced directory (or the same AAP
        # pair) while the orphan is still applying writes (see _fences).
        self._fences = FenceTracker(
            max_orphans=lambda: _job_config.MAX_ORPHANS,
            job_timeout=lambda: self.job_timeout,
        )
        # Fence-expiry requeue attempts per job (see _run): gated jobs that
        # outwait the bounded grace are requeued instead of failed so one
        # slow orphan does not burn the whole FIFO lane job by job. Bounded
        # so a never-draining fence still fails loudly instead of spinning.
        self._requeues: dict[str, int] = {}
        self._max_fence_requeues = 5
        # Off-lane wait-set for fence-gated jobs (#10): fence-gated jobs are
        # parked here (job_id -> {"work_dir", "pair_fp", "fence", "until"})
        # instead of burning the single FIFO worker on a bounded wait or
        # immediately requeueing to the back (which still head-of-line
        # blocks the lane on the next dequeue). The worker drains ready
        # parked jobs (unfenced or grace-expired) before each dequeue so
        # unrelated queued jobs run first while gated jobs wait off-lane.
        self._parked: dict[str, dict[str, Any]] = {}
        # Supervisor restart tracking: burst restarts trigger a bounded
        # backoff so a poison job cannot hot-spin the single worker.
        self._restart_times: list[float] = []
        self._draining = False
        self._worker = threading.Thread(target=self._run, name="api-job-worker", daemon=True)
        self._worker.start()

    # -- public API -----------------------------------------------------
    def submit(
        self,
        job_type: str,
        params: dict[str, Any],
        func: Callable[[JobRecord], dict[str, Any]],
        job_dir: str | None = None,
    ) -> JobRecord:
        """Enqueue a job and return its initial record.

        Args:
            job_type: Human-readable job category.
            params: Request parameters (stored for auditing).
            func: Worker ``func(job) -> result dict`` run in the FIFO thread.
            job_dir: Reuse an existing working directory (chained ETL phases)
                instead of creating a fresh isolated one.
        """
        self.ensure_worker()
        degraded = startup_degraded_reason()
        if degraded:
            # Startup probes latched degraded, but storage may have recovered
            # since (transient mount/perm delay): re-probe DB + job-dir
            # writability and clear the flag when healthy instead of 503ing
            # forever. Keeps AAP_BRIDGE_ALLOW_DEGRADED_STARTUP behavior
            # inside the helper (opt-in clears unconditionally). Only
            # storage-probe-shaped reasons (api-db:/job-dir:) trigger a
            # re-probe; arbitrary manual flags (e.g. tests) keep failing
            # fast without a probe side effect.
            is_storage_probe = ("api-db:" in degraded) or ("job-dir:" in degraded)
            if is_storage_probe:
                try:
                    if _job_config.clear_startup_degraded_if_recovered():
                        degraded = None
                    else:
                        degraded = startup_degraded_reason()
                except Exception:
                    degraded = startup_degraded_reason()
        if degraded:
            raise QueueFullError(
                f"Server storage unhealthy at startup ({degraded}); retry after it recovers"
            )
        if getattr(self, "_draining", False):
            raise QueueFullError("Server is shutting down; submissions closed")
        job_id = str(uuid.uuid4())
        if job_dir:
            work_dir = os.path.abspath(job_dir)
            base = os.path.abspath(self.base_dir)
            if os.path.commonpath([work_dir, base]) != base:
                raise ValueError("job_dir must stay under the job base directory")
        else:
            work_dir = os.path.join(self.base_dir, job_id)
        now = _utcnow()
        job: JobRecord = {
            "job_id": job_id,
            "job_type": job_type,
            "status": "queued",
            "params": dict(params),
            "job_dir": work_dir,
            "result": None,
            "error": None,
            "error_id": None,
            "exit_code": None,
            "created_at": now,
            "updated_at": now,
            "cancel_requested": False,
        }
        with self._lock:
            # Reap finished orphans first so a brief burst of slow-target
            # timeouts stops shedding load as soon as its threads drain,
            # instead of looking like a sustained outage until the next
            # dequeue cycle.
            queued = sum(1 for j in self._jobs.values() if j["status"] in ACTIVE_STATUSES)
            if queued >= _job_config.MAX_QUEUE_DEPTH:
                raise QueueFullError(
                    f"Job queue at capacity ({_job_config.MAX_QUEUE_DEPTH}); retry later"
                )
            pair_fp = params.get(SNAPSHOT_FP)
            fence_error = self._fences.check_submit(work_dir, str(pair_fp) if pair_fp else "")
            if fence_error is not None:
                raise QueueFullError(fence_error)
            self._jobs[job_id] = job
            self._funcs[job_id] = func
            evict_dirs = self._evict_locked()
        for victim_dir in evict_dirs:
            self._remove_job_dir(victim_dir, self.base_dir)
        try:
            os.makedirs(work_dir, exist_ok=True)
        except Exception:
            with self._lock:
                self._jobs.pop(job_id, None)
                self._funcs.pop(job_id, None)
            raise
        self._queue.put(job_id)
        return self._public(job)

    def wait_for_unfence(self, job_dir: str) -> bool:
        """Public wrapper for the bounded fence wait (workers outside the manager)."""
        return self._wait_for_unfence(job_dir)

    def shutdown_drain(self, timeout_secs: float | None = None) -> dict[str, int]:
        """Stop accepting submissions and bounded-wait for queued work.

        Sets the draining flag (new submits fail fast), then joins the FIFO
        queue up to *timeout_secs* (default from AAP_BRIDGE_DRAIN_SECS,
        30s). Returns {"pending": n, "running": m} for leftovers so
        lifespan can log what was dropped. Never raises.
        """
        if timeout_secs is None:
            timeout_secs = float(_env_float("AAP_BRIDGE_DRAIN_SECS", 30.0))
        self._draining = True
        import time as _time

        deadline = _time.monotonic() + max(float(timeout_secs), 0.0)
        while _time.monotonic() < deadline:
            with self._lock:
                pending = sum(1 for j in self._jobs.values() if j["status"] in ACTIVE_STATUSES)
            if pending == 0:
                break
            _time.sleep(0.2)
        try:
            with self._lock:
                pending = sum(1 for j in self._jobs.values() if j["status"] == "queued")
                running = sum(1 for j in self._jobs.values() if j["status"] == "running")
            return {"pending": pending, "running": running}
        except Exception:
            return {"pending": -1, "running": -1}

    def delete(self, job_id: str) -> None:
        """Delete a terminal job record and remove its directory.

        Raises KeyError/ValueError. When another job record (queued,
        running, **or terminal**) shares the same working directory (chained
        ETL), deletion of the record proceeds but the directory is kept so
        sibling jobs keep their artifacts; only the last record referencing
        a directory removes it. Filesystem cleanup never fails the call:
        on-disk leftovers are logged. Deleting a job that a queued or
        running chained child still references is rejected with ValueError
        (409) so accepted chains cannot turn into guaranteed Unknown job_id
        failures after waiting through the queue.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise UnknownJobError(self._unknown_job_message(job_id))
            if job["status"] not in TERMINAL_STATUSES:
                raise ConflictError(
                    f"Job '{job_id}' is {job['status']}; only terminal jobs can be deleted"
                )
            # Guard chained children: a queued/running job that references
            # this id (or shares its directory) still needs the record at
            # execution to resolve the workdir.
            for other_id, other in self._jobs.items():
                if other_id == job_id:
                    continue
                if other.get("status") not in ACTIVE_STATUSES:
                    continue
                if (other.get("params") or {}).get("job_id") == job_id:
                    raise ConflictError(
                        f"Job '{job_id}' is referenced by queued job '{other_id}'; "
                        "delete or wait for the chained job first"
                    )
            job_dir = job.get("job_dir", "")
            sharers = [
                jid
                for jid, other in self._jobs.items()
                if jid != job_id and other.get("job_dir") == job_dir
            ]
            fenced = bool(job_dir) and self._fences.is_fenced_dir(str(job_dir))
            del self._jobs[job_id]
            self._funcs.pop(job_id, None)
            self._requeues.pop(job_id, None)
            self._parked.pop(job_id, None)
        if job_dir:
            if sharers:
                log.warning(
                    "job dir for %s kept: shared with jobs %s",
                    job_id,
                    ",".join(sharers),
                )
                return
            if fenced:
                # A timed-out orphan is still applying writes into this
                # directory: drop the record but keep the directory (like
                # the shared-dir branch) so the orphan cannot recreate an
                # unowned tree or fail its writes noisily. The fence
                # releases when the orphan drains.
                log.warning(
                    "job dir for %s kept: fenced by a timed-out attempt "
                    "still running; directory survives until unfence",
                    job_id,
                )
                return
            self._remove_job_dir(str(job_dir), self.base_dir)

    def cancel(self, job_id: str) -> JobRecord:
        """Cancel a queued (or running) job.

        Queued jobs transition to ``cancelled`` immediately. Running jobs are
        flagged best-effort: the worker finishes the current phase (ETL phases
        are not preemptive) and then reports ``cancelled`` instead of
        ``succeeded``. Callers must treat a 200 on a running job as
        cancel-pending, not as proof no writes were applied.
        Raises KeyError when unknown, ValueError when already terminal.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise UnknownJobError(self._unknown_job_message(job_id))
            if job["status"] in TERMINAL_STATUSES:
                raise ConflictError(f"Job '{job_id}' is already {job['status']}")
            indexed: tuple[str, str, str] | None = None
            if job["status"] == "queued":
                job["status"] = "cancelled"
                job["error"] = "Cancelled by operator"
                job["updated_at"] = _utcnow()
                self._funcs.pop(job_id, None)
                self._requeues.pop(job_id, None)
                self._parked.pop(job_id, None)
                # Queued-cancel bypasses _set(): index the terminal
                # transition here so post-restart polls stay actionable.
                indexed = (job["job_id"], job["job_type"], "cancelled")
            else:
                job["cancel_requested"] = True
                job["updated_at"] = _utcnow()
                # Best-effort pollable signal so future resume/retry phases
                # can detect a pending cancel without invasive phase polling.
                try:
                    marker_dir = job.get("job_dir", "")
                    if marker_dir:
                        with open(os.path.join(str(marker_dir), ".cancel_requested"), "w") as fh:
                            fh.write(_utcnow())
                except Exception:
                    pass
            public = self._public(job)
        if indexed is not None:
            self._record_terminal_index(*indexed)
        return public

    def ensure_worker(self) -> None:
        """Restart a dead worker thread (supervisor).

        Guarded by a dedicated lock so concurrent restarts cannot spawn
        duplicate workers. Any job left ``running`` on a dead worker is
        marked failed with a restart error_id instead of orphaning forever;
        queued ids stay in the queue for the replacement worker.
        """
        if self._worker.is_alive():
            return
        import time as _time

        with self._worker_lock:
            if self._worker.is_alive():
                return
            now = _time.monotonic()
            self._restart_times = [t for t in self._restart_times if now - t < 60.0]
            self._restart_times.append(now)
            burst = len(self._restart_times) > 5
            stale_worker = self._worker
        # Join outside the lock: the stale thread is dead, so this returns
        # fast and never serializes concurrent submitters behind a sick
        # worker. The burst backoff sleep also runs inside the replacement
        # worker thread (see _delayed_run), never on the caller thread, so
        # submit floods during degradation shed via queueing instead of
        # occupying sync-route threads for 5s each.
        if burst:
            log.error(
                "api-job-worker restart burst (%d in 60s); replacement worker "
                "backs off 5s before consuming",
                len(self._restart_times),
            )
        try:
            stale_worker.join(timeout=5)
        except Exception:
            pass
        with self._worker_lock:
            if self._worker.is_alive():
                return
            error_id = uuid.uuid4().hex[:12]
            log.error("api-job-worker died; restarting supervisor thread")
            reaped: list[tuple[str, str]] = []
            with self._lock:
                for jid, job in list(self._jobs.items()):
                    if job.get("status") == "running":
                        job["status"] = "failed"
                        job["error"] = (
                            f"Worker restarted mid-execution (error_id={error_id}); "
                            "see server logs."
                        )
                        job["error_id"] = error_id
                        job["updated_at"] = _utcnow()
                        self._funcs.pop(jid, None)
                        # Supervisor-restart bypasses _set(): collect for
                        # indexing below so post-restart polls stay actionable.
                        reaped.append((jid, str(job.get("job_type", ""))))
            if burst:
                self._worker = threading.Thread(
                    target=self._delayed_run, args=(5.0,), name="api-job-worker", daemon=True
                )
            else:
                self._worker = threading.Thread(
                    target=self._run, name="api-job-worker", daemon=True
                )
            self._worker.start()
        for jid, jtype in reaped:
            self._record_terminal_index(jid, jtype, "failed")

    def _delayed_run(self, delay_secs: float) -> None:
        """Start :meth:`_run` after a bounded backoff (supervisor restarts).

        Runs on the replacement worker thread so the restart-burst backoff
        never occupies a request thread. ``is_alive()`` is true from
        ``start()``, so concurrent ``ensure_worker`` calls during the delay
        return early instead of spawning duplicate workers.
        """
        import time as _time

        try:
            _time.sleep(max(float(delay_secs), 0.0))
        except Exception:
            pass
        self._run()

    # -- internals ------------------------------------------------------
    @staticmethod
    def _public(job: JobRecord) -> JobRecord:
        # Never expose server-local paths; internal callers use get_internal().
        # Internal snapshot keys (``_snapshot_*``) are also stripped: they pin
        # execution-time connection resolution, not client input.
        params = {k: v for k, v in job["params"].items() if not k.startswith("_")}
        return {
            "job_id": job["job_id"],
            "job_type": job["job_type"],
            "status": job["status"],
            "params": params,
            "result": job["result"],
            "error": job["error"],
            "exit_code": job["exit_code"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
        }

    @staticmethod
    def _remove_job_dir(job_dir: str, base_dir: str) -> None:
        """Best-effort confined removal of one job directory (see _retention)."""
        from aap_migration.api.jobs import _retention as _retention_mod

        _retention_mod.remove_job_dir(job_dir, base_dir)

    def _evict_locked(self) -> tuple[str, ...]:
        """Enforce MAX_JOBS retention: drop oldest terminal jobs first (see _retention).

        Callers hold ``self._lock``. Returns confined directories safe to
        remove after releasing the lock.
        """
        from aap_migration.api.jobs import _retention as _retention_mod

        return _retention_mod.evict_locked(
            self._jobs,
            self._funcs,
            self._requeues,
            self._fences.fenced_dirs_snapshot(),
            _job_config.MAX_JOBS,
            ACTIVE_STATUSES,
            TERMINAL_STATUSES,
        )

    def _reap_orphans_locked(self) -> None:
        """Drop finished orphans and unfence their directories (see _fences).

        Kept for callers that already hold ``self._lock``; the tracker owns
        its own lock with consistent manager -> tracker ordering.
        """
        self._fences.reap()

    def _reap_orphans(self) -> None:
        """Non-blocking orphan reap (lock-guarded)."""
        self._fences.reap()

    def _wait_for_unfenced(self, key: str, kind: str) -> bool:
        """Block until *key* is unfenced or the grace period expires."""
        return bool(self._fences.wait_unfenced(key, kind))

    def _wait_for_unfence(self, job_dir: str) -> bool:
        """Block until *job_dir* is unfenced or the grace period expires."""
        return bool(self._fences.wait_unfenced(job_dir, "dir"))

    def _wait_for_unfence_pair(self, pair_fp: str) -> bool:
        """Block until *pair_fp* is unfenced or the grace period expires.

        Pair-level twin of :meth:`_wait_for_unfence`: a resubmitted job on a
        fresh directory must not run concurrently with a timed-out orphan
        still applying writes to the same AAP pair. Same bounded grace and
        orphan-pressure fail-fast semantics.
        """
        return bool(self._fences.wait_unfenced(pair_fp, "pair"))

    def _fence_grace_secs(self) -> float:
        """Bounded fence grace for off-lane parking (mirrors wait_unfenced)."""
        try:
            grace = min(float(self.job_timeout), 120.0)
        except (TypeError, ValueError):
            grace = 120.0
        return max(grace, 1.0)

    def _drain_parked(self) -> None:
        """Requeue parked jobs whose fence cleared or whose grace expired.

        Unfenced jobs go back to the main FIFO so they run next; still-
        fenced jobs whose park deadline passed are re-parked (bounded) or
        failed via :meth:`_requeue_or_fail`. Never raises.
        """
        import time as _time

        try:
            parked = list(self._parked.items())
        except Exception:
            return
        if not parked:
            return
        self._reap_orphans()
        try:
            fenced_dirs, fenced_pairs = self._fences.fenced_snapshot()
        except Exception:
            return
        now = _time.monotonic()
        for job_id, info in parked:
            try:
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is None or job.get("status") != "queued":
                        self._parked.pop(job_id, None)
                        continue
                    work_dir = str(info.get("work_dir") or job.get("job_dir") or "")
                    pair_fp = str(info.get("pair_fp") or "")
                    fence = str(info.get("fence") or "workdir")
                    until = float(info.get("until") or 0.0)
                dir_fenced = bool(work_dir) and work_dir in fenced_dirs
                pair_fenced = bool(pair_fp) and pair_fp in fenced_pairs
                still_fenced = dir_fenced or pair_fenced
                if not still_fenced:
                    with self._lock:
                        self._parked.pop(job_id, None)
                    log.info("job %s unfenced while parked; requeued", job_id)
                    self._queue.put(job_id)
                    continue
                if now >= until:
                    # Grace expired while still fenced: bounded re-park/fail.
                    self._requeue_or_fail(job_id, work_dir, fence)
            except Exception:
                continue

    def _requeue_or_fail(self, job_id: str, work_dir: str, fence: str) -> bool:
        """Park a fence-gated job off-lane or fail it after repeated expiries.

        Returns True when the caller should ``continue`` (either way: the
        job was parked off-lane for a later drain, or it was failed and
        there is nothing more to do for this dequeue). Parked jobs keep
        status ``queued`` and wait in ``_parked`` (not the main FIFO) so
        unrelated queued jobs run first; :meth:`_drain_parked` requeues
        them when unfenced or re-parks/fails them on grace expiry. The
        ``_max_fence_requeues`` bound is preserved: after repeated expiries
        the job fails loudly instead of spinning forever. Status
        transitions are unchanged (queued -> queued on park, queued ->
        failed on expiry) so existing fence tests keep passing.
        """
        import time as _time

        with self._lock:
            # Drop stale park entries for jobs that left queued state
            # (cancelled/deleted/running) so they can never resurrect.
            job = self._jobs.get(job_id)
            if job is None or job.get("status") != "queued":
                self._parked.pop(job_id, None)
                return True
            attempts = self._requeues.get(job_id, 0)
            if attempts < self._max_fence_requeues:
                self._requeues[job_id] = attempts + 1
                try:
                    pair_fp = str((job.get("params") or {}).get(SNAPSHOT_FP) or "")
                except Exception:
                    pair_fp = ""
                self._parked[job_id] = {
                    "work_dir": work_dir,
                    "pair_fp": pair_fp,
                    "fence": fence,
                    "until": _time.monotonic() + self._fence_grace_secs(),
                }
                requeued = True
            else:
                self._requeues.pop(job_id, None)
                self._parked.pop(job_id, None)
                requeued = False
        if requeued:
            log.warning(
                "job %s still fenced (%s); parked off-lane (%d/%d) "
                "so unrelated queued jobs run first",
                job_id,
                fence,
                attempts + 1,
                self._max_fence_requeues,
            )
            return True
        self._fail(
            job_id,
            work_dir,
            "",
            f"{'Workdir' if fence == 'workdir' else 'AAP pair'} fenced by a "
            "timed-out attempt still running after repeated waits; "
            "resubmit after it drains",
        )
        return True

    def _set(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:  # deleted while running; nothing to update
                return
            for key, value in fields.items():
                job[key] = value  # type: ignore[literal-required]
            job["updated_at"] = _utcnow()
            if job["status"] in TERMINAL_STATUSES:
                self._requeues.pop(job_id, None)
                # Terminal jobs leave the off-lane wait-set too so a
                # parked entry can never resurrect them.
                self._parked.pop(job_id, None)
            indexed = (
                (job["job_id"], job["job_type"], job["status"])
                if job["status"] in TERMINAL_STATUSES
                else None
            )
        if indexed is not None:
            self._record_terminal_index(*indexed)

    def set_job_dir(self, job_id: str, job_dir: str) -> None:
        """Persist a pair-switch fork so later phases follow it (see #4).

        Called once from the worker lifecycle when ``setup_chained`` forks
        a fresh sibling dir: without this, chained phases and the artifact
        APIs keep resolving the stale parent dir. The fork stays under the
        job base directory by construction (sibling of the recorded dir).
        """
        self._set(job_id, job_dir=job_dir)

    def _live_job_dir(self, job_id: str, fallback: str) -> str:
        """Return the current job_dir, following a pair-switch fork.

        The worker captures ``job_dir`` before the fork; ``setup_chained``
        updates the record to the fresh sibling via :meth:`set_job_dir`.
        Console persistence, failure records, and orphan fences must use
        the live value, otherwise consoles land in the parent while reads
        serve the fork (and timeout fences park the wrong dir).
        Falls back to *fallback* when the record is gone.
        """
        try:
            with self._lock:
                current = self._jobs.get(job_id) or {}
                live = current.get("job_dir") or fallback
                return str(live)
        except Exception:
            return fallback

    # -- restart reconciliation -----------------------------------------
    # -- restart reconciliation (see _index) ------------------------------
    def _index_path(self) -> str:
        return self._index.path

    def _record_terminal_index(self, job_id: str, job_type: str, status: str) -> None:
        """Append a terminal summary so restarts stay actionable (best-effort)."""
        self._index.record_terminal(job_id, job_type, status, _utcnow())

    def _compact_terminal_index(self) -> None:
        """Keep the newest summaries (bounded retention, never raises)."""
        self._index.compact()

    def _unknown_job_message(self, job_id: str) -> str:
        """Actionable unknown-id error: name a pre-restart id as resubmittable."""
        return self._index.unknown_message(job_id)

    def _log_stale_job_dirs(self) -> None:
        """Count orphaned job dirs from a previous run (observability only)."""
        self._index.log_stale_dirs()

    def _fail(
        self,
        job_id: str,
        job_dir: str,
        output: str,
        message: str,
        exit_code: int | None = None,
    ) -> None:
        """Persist console, mint error_id, log, and mark a job failed."""
        error_id = uuid.uuid4().hex[:12]
        log.error("job %s failed (error_id=%s)", job_id, error_id)
        bounded = _bounded_output(output)
        _persist_console(job_dir, bounded, job_id)
        self._set(
            job_id,
            status="failed",
            error=f"{message} (error_id={error_id}); see server logs. " + _console_tail(bounded),
            error_id=error_id,
            exit_code=exit_code,
        )

    def _run(self) -> None:
        while True:
            # Drain off-lane parked jobs first so unfenced work returns to
            # the FIFO before new dequeues; expired parks re-park/fail here.
            try:
                self._drain_parked()
            except Exception:
                pass
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            pool = None
            timed_out = False
            # Promptly release fences whose orphans already finished.
            self._reap_orphans()
            try:
                with self._lock:
                    func = self._funcs.get(job_id)
                    job = self._jobs.get(job_id)
                    if job is None or func is None:
                        continue
                    if job["status"] == "cancelled":
                        # Cancelled while queued: never resurrect (the
                        # status check and dequeue gate share this lock).
                        continue
                    if job["status"] != "queued":
                        continue
                    # Skip jobs parked off-lane (still waiting): they were
                    # dequeued before parking via _requeue_or_fail's park
                    # path in a previous cycle, or are awaiting drain.
                    # Parked ids stay queued but must not run until
                    # _drain_parked requeues them (unfenced or grace expiry).
                    if job_id in self._parked:
                        continue
                    work_dir = str(job.get("job_dir", ""))
                    pair_fp = str((job.get("params") or {}).get(SNAPSHOT_FP) or "")
                    # Fence membership is read outside the manager lock
                    # (tracker-owned, one snapshot); parking below is the
                    # real guard, so a fence landing here only delays the
                    # job off-lane instead of running it concurrently.
                    fenced_dirs, fenced_pairs = self._fences.fenced_snapshot()
                    dir_fenced = work_dir in fenced_dirs
                    pair_fenced = bool(pair_fp) and pair_fp in fenced_pairs
                    fenced = dir_fenced or pair_fenced
                    if not fenced:
                        # Queued -> running transitions under the same lock
                        # as the cancel check, so a cancel landing here
                        # cannot be overwritten by the worker below.
                        job["status"] = "running"
                        job["updated_at"] = _utcnow()
                if fenced:
                    # A timed-out orphan is still applying writes on this
                    # directory -- or on this AAP pair from a resubmitted
                    # job on a fresh directory: park off-lane (bounded)
                    # instead of burning the single FIFO worker on a
                    # blocking wait, letting unrelated queued jobs run
                    # first. _drain_parked requeues when unfenced; after
                    # repeated expiries the job fails loudly instead of
                    # spinning forever.
                    fence_kind = "workdir" if dir_fenced else "pair"
                    if self._requeue_or_fail(job_id, work_dir, fence_kind):
                        continue
                    continue
                with self._lock:
                    job = self._jobs[job_id]
                    job_snapshot = cast(JobRecord, dict(job))
                job_dir = str(job_snapshot.get("job_dir", ""))
                buffer = StringIO()
                run_func: Callable[[JobRecord], dict[str, Any]] = func

                def _wrapper(
                    _buffer: StringIO = buffer,
                    _func: Callable[[JobRecord], dict[str, Any]] = run_func,
                    _snap: JobRecord = job_snapshot,
                ) -> dict[str, Any]:
                    # Re-point sys.stdout/stderr at the proxies for the
                    # worker's duration: test harnesses (and any host) may
                    # have replaced the module-install-time streams, which
                    # would otherwise bypass per-job capture entirely.
                    import sys as _sys

                    _prev_out, _prev_err = _sys.stdout, _sys.stderr
                    _sys.stdout, _sys.stderr = _stdout_proxy, _stderr_proxy
                    _stdout_proxy.bind(_buffer)
                    _stderr_proxy.bind(_buffer)
                    try:
                        return _func(_snap)
                    finally:
                        _stdout_proxy.unbind()
                        _stderr_proxy.unbind()
                        _sys.stdout, _sys.stderr = _prev_out, _prev_err

                pool = daemon_thread_pool(max_workers=1, thread_name_prefix="api-job-run")
                future = pool.submit(_wrapper)
                try:
                    try:
                        result = future.result(timeout=self.job_timeout)
                    except concurrent.futures.TimeoutError:
                        timed_out = True
                        with self._lock:
                            current = self._jobs.get(job_id)
                            cancelled = bool(current and current.get("cancel_requested"))
                        output = _bounded_output(buffer.getvalue())
                        live_dir = self._live_job_dir(job_id, job_dir)
                        _persist_console(live_dir, output, job_id)
                        error_id = uuid.uuid4().hex[:12]
                        log.error(
                            "job %s timed out after %ss (error_id=%s)",
                            job_id,
                            self.job_timeout,
                            error_id,
                        )
                        if cancelled:
                            self._set(
                                job_id,
                                status="cancelled",
                                error="Cancelled by operator (timed out while cancelling)",
                            )
                        else:
                            self._set(
                                job_id,
                                status="failed",
                                error=f"Job timed out after {self.job_timeout:g}s "
                                f"(error_id={error_id}); see server logs. " + _console_tail(output),
                                error_id=error_id,
                            )
                        try:
                            # No-op once the attempt is running, but harmless
                            # when it has not started yet.
                            future.cancel()
                        except Exception:
                            pass
                        # The pool thread cannot be preempted and keeps
                        # applying writes: fence its directory and its AAP
                        # pair, and track the orphan so the next dequeue
                        # waits (bounded) instead of running concurrently,
                        # and so resume/retry onto the failed job cannot
                        # share the dir mid-write. Pair fencing also gates
                        # resubmissions on a fresh directory: the conflict
                        # domain is the AAP pair, not the workdir.
                        with self._lock:
                            timed_out_params = (self._jobs.get(job_id) or {}).get("params") or {}
                            timed_out_fp = timed_out_params.get(SNAPSHOT_FP)
                        self._fences.note_timeout(
                            future=future,
                            pool=pool,
                            job_dir=live_dir,
                            pair_fp=str(timed_out_fp) if timed_out_fp else None,
                        )
                        pool = None
                        continue
                    with self._lock:
                        current = self._jobs.get(job_id)
                        cancelled = bool(current and current.get("cancel_requested"))
                    output = _bounded_output(buffer.getvalue())
                    live_dir = self._live_job_dir(job_id, job_dir)
                    _persist_console(live_dir, output, job_id)
                    if cancelled:
                        self._set(
                            job_id,
                            status="cancelled",
                            error="Cancelled by operator (best-effort: the workflow is not "
                            "preemptive, so target writes may already be applied; "
                            "verify before resubmitting to avoid replaying them)",
                            result=_normalize_result(result),
                        )
                    else:
                        self._set(job_id, status="succeeded", result=_normalize_result(result))
                except click.exceptions.Exit as exc:
                    output = _bounded_output(buffer.getvalue())
                    code = int(exc.exit_code or 0)
                    live_dir = self._live_job_dir(job_id, job_dir)
                    if code == 0:
                        _persist_console(live_dir, output, job_id)
                        self._set(
                            job_id,
                            status="succeeded",
                            result={"message": "Completed (exit 0)", "artifacts": []},
                            exit_code=0,
                        )
                    else:
                        self._fail(
                            job_id,
                            live_dir,
                            output,
                            f"Command failed with exit {code}",
                            exit_code=code,
                        )
                except click.ClickException as exc:
                    log.error("job %s click error: %s", job_id, exc)
                    try:
                        detail = exc.format_message()
                    except Exception:
                        detail = str(exc)
                    message = detail.strip() or "Job failed"
                    # Truncate: polled payloads carry the actionable line;
                    # full tracebacks stay server-side.
                    live_dir = self._live_job_dir(job_id, job_dir)
                    self._fail(job_id, live_dir, _bounded_output(buffer.getvalue()), message[:500])
                except (ValueError, KeyError) as exc:
                    # Fail-fast domain (unknown job, pair drift, deleted
                    # connection, corrupt snapshot, missing pin keys):
                    # raised by our own guards with operator-actionable,
                    # secret-free text. Preserve it so queued jobs fail
                    # with guidance instead of a bare class name. Every
                    # other exception kind stays limited to its bare name
                    # below (backend detail must not leak). UnknownJobError
                    # is a KeyError subclass, so chained-reference misses
                    # keep their "Unknown job_id ..." detail here.
                    log.error("job %s failed: %s", job_id, exc)
                    try:
                        detail = str(exc).strip()
                    except Exception:
                        detail = ""
                    message = (
                        f"{type(exc).__name__}: {detail}"[:500]
                        if detail
                        else f"{type(exc).__name__}"
                    )
                    live_dir = self._live_job_dir(job_id, job_dir)
                    self._fail(
                        job_id,
                        live_dir,
                        _bounded_output(buffer.getvalue()),
                        message,
                    )
                except Exception as exc:  # noqa: BLE001 - surfaced via polling
                    log.exception("job %s failed", job_id)
                    live_dir = self._live_job_dir(job_id, job_dir)
                    self._fail(
                        job_id,
                        live_dir,
                        _bounded_output(buffer.getvalue()),
                        f"{type(exc).__name__}",
                    )
                except BaseException as exc:  # keep the single worker alive
                    try:
                        live_dir = self._live_job_dir(job_id, job_dir)
                        _persist_console(live_dir, _bounded_output(buffer.getvalue()), job_id)
                    except Exception:
                        pass
                    error_id = uuid.uuid4().hex[:12]
                    log.exception("job %s base-exception (error_id=%s)", job_id, error_id)
                    self._set(
                        job_id,
                        status="failed",
                        error=f"Worker error (error_id={error_id}); see server logs.",
                        error_id=error_id,
                    )
                    if isinstance(exc, KeyboardInterrupt | SystemExit):
                        raise
            finally:
                if pool is not None:
                    try:
                        pool.shutdown(wait=not timed_out, cancel_futures=timed_out)
                    except Exception:
                        pass
                self._queue.task_done()
