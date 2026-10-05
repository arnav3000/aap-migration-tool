"""Thread-safe FIFO background job manager.

Jobs run sequentially in a single FIFO worker thread. Sequential execution is
deliberate: migration phases rely on per-job config files and chained working
directories, and concurrent migrations against the same AAP pair would
conflict.

Lifecycle: ``queued -> running -> succeeded | failed | cancelled``.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import uuid
from collections.abc import Callable
from typing import Any

from aap_migration.api.jobs import _config as _job_config
from aap_migration.api.jobs._config import _env_float, startup_degraded_reason
from aap_migration.api.jobs._console import (
    _bounded_output,
    _console_tail,
    _persist_console,
)
from aap_migration.api.jobs._fences import FenceTracker
from aap_migration.api.jobs._index import RestartIndex
from aap_migration.api.jobs._reads import JobReadMixin
from aap_migration.api.jobs._records import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    ConflictError,
    JobRecord,
    QueueFullError,
    ServerShuttingDownError,
    StorageUnhealthyError,
    UnknownJobError,
    _utcnow,
)
from aap_migration.api.jobs._worker import JobWorkerMixin
from aap_migration.api.store import SNAPSHOT_FP, SNAPSHOT_STABLE

log = logging.getLogger("aap_migration.api.jobs")


class JobManager(JobReadMixin, JobWorkerMixin):
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
        # Singleflight for degraded-storage re-probes (P1 #8): concurrent
        # submits while degraded share one probe instead of each blocking
        # up to 5s on the requesting thread and hammering the same broken
        # storage. Guarded by _probe_guard; _probe_event is None when no
        # probe is in flight.
        self._probe_guard = threading.Lock()
        self._probe_event: threading.Event | None = None
        self._probe_box: dict[str, Any] = {}
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
                    if self._clear_degraded_bounded():
                        degraded = None
                    else:
                        degraded = startup_degraded_reason()
                except Exception:
                    degraded = startup_degraded_reason()
        if degraded:
            raise StorageUnhealthyError(
                f"Server storage unhealthy at startup ({degraded}); retry after it recovers"
            )
        if getattr(self, "_draining", False):
            raise ServerShuttingDownError("Server is shutting down; submissions closed")
        # Cancel fence (P1 #3): chaining (resume/retry/resubmit via
        # job_id) onto a job that was cancelled mid-phase replays writes the
        # pool thread already applied. Require explicit force=true (where the
        # endpoint supports it); otherwise refuse with a 409 naming the
        # stop-point. Unchained submits and chains onto succeeded/failed or
        # queued-cancelled jobs are unaffected.
        self.assert_no_cancel_fence(params.get("job_id"), bool(params.get("force", False)))
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
            queued = self._admission_count_locked()
            if queued >= _job_config.MAX_QUEUE_DEPTH:
                raise QueueFullError(
                    f"Job queue at capacity ({_job_config.MAX_QUEUE_DEPTH}); retry later"
                )
            # Total-growth bound (includes parked): admission excludes
            # off-lane parked waiters so healthy submits are not shed while
            # the lane idles, but total non-terminal work (parked + queued +
            # running) must not grow without bound across fence bursts.
            total_active = self._total_active_locked()
            total_cap = _job_config.MAX_QUEUE_DEPTH + _job_config.MAX_ORPHANS
            if total_active >= total_cap:
                raise QueueFullError(f"Job queue at total capacity ({total_cap}); retry later")
            pair_fp = params.get(SNAPSHOT_FP)
            stable_fp = params.get(SNAPSHOT_STABLE)
            fence_error = self._fences.check_submit(
                work_dir,
                str(pair_fp) if pair_fp else "",
                str(stable_fp) if stable_fp else None,
            )
            if fence_error is not None:
                raise QueueFullError(fence_error)
            self._jobs[job_id] = job
            self._funcs[job_id] = func
            evict_dirs = self._evict_locked()
            live_dirs = {str(j.get("job_dir", "")) for j in self._jobs.values() if j.get("job_dir")}
            fenced_now = self._fences.fenced_dirs_snapshot()
        for victim_dir in evict_dirs:
            self._remove_job_dir(victim_dir, self.base_dir)
        # Reclaim record-less, drained orphan dirs (P3 #27): a timed-out
        # attempt whose record was deleted/evicted leaves its directory
        # behind by design (the orphan may still be writing). Once the
        # orphan drains (unfenced) and no record references the dir, move
        # it to a bounded quarantine instead of growing the job dir
        # forever. Fenced and live dirs are never touched.
        from aap_migration.api.jobs import _retention as _retention_mod

        _retention_mod.sweep_orphan_dirs(
            self.base_dir,
            live_dirs,
            fenced_now,
            max_age_secs=_job_config.ORPHAN_SWEEP_AGE_SECS,
            quarantine_max=_job_config.ORPHAN_QUARANTINE_MAX,
        )
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
        30s). Leftover queued jobs transition to ``cancelled`` and leftover
        running jobs to ``failed`` with an actionable
        "interrupted; verify before resubmitting" error (P2 #19), so a
        post-restart poll never reports a bare 404 for work the server
        dropped: every leftover id is either still polled in this process
        or present in the restart index. Returns {"pending": n, "running":
        m} for leftovers so lifespan can log what was dropped. Never
        raises.
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
                leftover = [
                    (jid, str(j.get("status")))
                    for jid, j in self._jobs.items()
                    if j.get("status") in ACTIVE_STATUSES
                ]
            dropped_queued = sum(1 for _, status in leftover if status == "queued")
            dropped_running = sum(1 for _, status in leftover if status != "queued")
            for jid, status in leftover:
                if status == "queued":
                    self._set(
                        jid,
                        status="cancelled",
                        error=(
                            "Interrupted by server shutdown before execution; "
                            "verify target state before resubmitting."
                        ),
                        shutdown_interrupted=True,
                    )
                else:
                    error_id = uuid.uuid4().hex[:12]
                    self._set(
                        jid,
                        status="failed",
                        error=(
                            "Interrupted by server shutdown while running "
                            f"(error_id={error_id}); verify target state before "
                            "resubmitting; see server logs."
                        ),
                        error_id=error_id,
                        shutdown_interrupted=True,
                    )
            # Report what was dropped (pre-transition counts): post-transition
            # both are zero by construction, which would make the lifespan
            # log useless.
            return {"pending": dropped_queued, "running": dropped_running}
        except Exception:
            return {"pending": -1, "running": -1}

    def fail_fast(self, job_id: str, message: str) -> bool:
        """Mark a queued job failed immediately with an actionable message.

        Used when a submit-time guarantee broke between enqueue and the
        post-enqueue recheck (P1 #4, active-pair move in the submit
        window): the job is already doomed, so fail it now instead of
        letting it wait out the queue and fail at execution. Terminal
        indexing runs through :meth:`_set`, so post-restart polls of the
        id stay actionable. Returns False when the job already left
        queued state (nothing done).
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.get("status") != "queued":
                return False
            self._parked.pop(job_id, None)
            self._requeues.pop(job_id, None)
        self._set(job_id, status="failed", error=message)
        return True

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
                job["cancel_requested_at"] = _utcnow()  # type: ignore[typeddict-unknown-key]
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

    def assert_no_cancel_fence(self, ref_job_id: str | None, force: bool = False) -> None:
        """Refuse chained resubmission onto a mid-phase-cancelled job without force.

        Cancel of a running job only sets ``cancel_requested``: the pool
        thread finishes the current phase and then records
        cancelled-with-result, so target writes may already be applied.
        Chaining (resume/retry/resubmit via ``job_id``) onto such a job
        without an explicit ``force=true`` risks replaying those writes.

        Raises:
            ConflictError: When the referenced job was cancelled mid-phase
                (``cancelled_at_phase`` marker present) and *force* is false.
                Queued cancels (no writes) and succeeded/failed references
                never trip the fence; unknown ids are left for the caller's
                404 path.
        """
        if not ref_job_id or force:
            return
        with self._lock:
            ref = self._jobs.get(ref_job_id)
            if ref is None:
                return
            if ref.get("status") != "cancelled":
                return
            marker = ref.get("cancelled_at_phase")
            if not marker:
                return
            completed = ref.get("completed_phases") or []
        raise ConflictError(
            f"Job '{ref_job_id}' was cancelled mid-phase ({marker}; "
            f"completed before stop: {completed or 'unknown'}); chained "
            "resume/retry may replay writes already applied. Resubmit with "
            "force=true to acknowledge, or submit without job_id for a fresh directory."
        )

    def _clear_degraded_bounded(self, timeout_secs: float | None = None) -> bool:
        """Re-probe startup storage with a hard timeout (never hang submit).

        ``clear_startup_degraded_if_recovered`` does blocking FS/DB I/O with
        no timeout of its own; run it on a daemon thread and wait at most
        *timeout_secs* (default ~5s, ``AAP_BRIDGE_DEGRADED_PROBE_SECS``).
        On timeout (or error, handled by the caller) return False so the
        submission keeps the degraded state and fails fast instead of
        occupying a sync-route thread indefinitely.

        Singleflight (P1 #8): concurrent submits while degraded share one
        in-flight probe instead of each spawning a thread that blocks up to
        5s and hammers the same broken storage -- during exactly the outage
        the degraded flag exists to contain, per-submit probes convert a
        storage slowdown into threadpool exhaustion.
        """
        if timeout_secs is None:
            try:
                timeout_secs = float(_env_float("AAP_BRIDGE_DEGRADED_PROBE_SECS", 5.0))
            except Exception:
                timeout_secs = 5.0
        timeout_secs = max(float(timeout_secs), 0.1)
        import time as _time

        deadline = _time.monotonic() + timeout_secs
        with self._probe_guard:
            event = self._probe_event
            if event is None:
                event = threading.Event()
                box: dict[str, Any] = {}
                self._probe_event = event
                self._probe_box = box
                thread = threading.Thread(
                    target=self._run_degraded_probe,
                    args=(box, event),
                    name="degraded-probe",
                    daemon=True,
                )
                thread.start()
            else:
                box = self._probe_box
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            return False
        if not event.wait(timeout=remaining):
            log.warning(
                "startup storage re-probe timed out after %ss; keeping degraded state",
                timeout_secs,
            )
            # Orphan the hung probe so it cannot pin later submits: the
            # probe thread clears _probe_event only when it still owns it
            # (see _run_degraded_probe), so detaching here lets the next
            # submit start a fresh probe after recovery instead of joining
            # the dead one and 503ing until restart.
            with self._probe_guard:
                if self._probe_event is event:
                    self._probe_event = None
                    self._probe_box = {}
            return False
        if "error" in box:
            raise box["error"]
        return bool(box.get("ok", False))

    def _run_degraded_probe(self, box: dict[str, Any], event: threading.Event) -> None:
        """Run one shared degraded-storage probe, then wake all waiters."""
        try:
            try:
                box["ok"] = _job_config.clear_startup_degraded_if_recovered()
            except Exception as exc:  # caller falls back to the cached reason
                box["error"] = exc
        finally:
            with self._probe_guard:
                if self._probe_event is event:
                    self._probe_event = None
                    self._probe_box = {}
            event.set()

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

    # -- internals ------------------------------------------------------

    def _admission_count_locked(self) -> int:
        """Jobs consuming the shared admission cap (callers hold the lock).

        Parked fence-waiters (P1 #9) are off-lane: they wait in _parked,
        not on the worker, so they must not consume the shared admission
        cap -- otherwise ~100 parked same-pair waiters shed healthy
        unrelated submits with 429 while the lane itself idles. Every
        other active-job scan (drain, guards, pressure) still counts
        parked jobs as active work; only admission excludes them.

        Total growth is bounded separately (see _total_active_locked):
        admission exclusion never lets total non-terminal work exceed
        MAX_QUEUE_DEPTH + MAX_ORPHANS, so parked bursts shed with an
        observable 429 instead of growing _jobs without bound.
        """
        return sum(
            1
            for jid, j in self._jobs.items()
            if j["status"] in ACTIVE_STATUSES and jid not in self._parked
        )

    def _total_active_locked(self) -> int:
        """All queued + running jobs including parked (callers hold lock).

        Single shared active-set definition with _admission_count_locked:
        both scan ACTIVE_STATUSES; admission subtracts parked, this one
        does not. Used as the total-growth bound so parked accumulation
        across fence bursts cannot exceed MAX_QUEUE_DEPTH + MAX_ORPHANS.
        """
        return sum(1 for j in self._jobs.values() if j["status"] in ACTIVE_STATUSES)

    @staticmethod
    def _public(job: JobRecord) -> JobRecord:
        # Never expose server-local paths; internal callers use get_internal().
        # Internal snapshot keys (``_snapshot_*``) are also stripped via the
        # single home in _records so router and manager views cannot diverge.
        from aap_migration.api.jobs._records import public_job_params

        params = public_job_params(dict(job["params"]))
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

    def _set(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:  # deleted while running; nothing to update
                return
            if job.get("shutdown_interrupted") and "status" in fields:
                # Shutdown already gave this job its terminal verdict (P2
                # #19): a pool thread finishing afterwards must not
                # overwrite the interrupted message with a normal
                # succeeded/failed transition.
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
    def _record_terminal_index(self, job_id: str, job_type: str, status: str) -> None:
        """Append a terminal summary so restarts stay actionable (best-effort)."""
        self._index.record_terminal(job_id, job_type, status, _utcnow())

    def _unknown_job_message(self, job_id: str) -> str:
        """Actionable unknown-id error: name a pre-restart id as resubmittable."""
        return self._index.unknown_message(job_id)

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
