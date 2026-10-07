"""FIFO worker loop + fence/park orchestration (see FenceTracker).

Split from :mod:`aap_migration.api.jobs.manager` so the manager facade
stays under 1k lines: this mixin owns the worker thread (``_run``),
off-lane parking (``_drain_parked`` / ``_requeue_or_fail``), fence grace
and TTL helpers, and the restart backoff (``_delayed_run``). ``JobManager``
inherits it; behavior is unchanged.
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import uuid
from collections.abc import Callable
from io import StringIO
from typing import TYPE_CHECKING, Any, TypeVar, cast

import click

from aap_migration.api.jobs._console import (
    _bounded_output,
    _console_tail,
    _persist_console,
    _stderr_proxy,
    _stdout_proxy,
)
from aap_migration.api.jobs._fences import daemon_thread_pool
from aap_migration.api.jobs._records import JobRecord, _normalize_result, _utcnow
from aap_migration.api.jobs._scrub import _scrub_output
from aap_migration.api.store import SNAPSHOT_FP, SNAPSHOT_STABLE

if TYPE_CHECKING:
    import queue as _queue_mod
    import threading as _threading_mod

    from aap_migration.api.jobs._fences import FenceTracker

log = logging.getLogger("aap_migration.api.jobs")

_T = TypeVar("_T")


class JobWorkerMixin:
    """Worker-loop + park/fence orchestration for :class:`JobManager`."""

    # Owned by JobManager.__init__; declared here so mypy sees the mixin
    # contract without a cyclic import (same pattern as JobReadMixin).
    _fences: FenceTracker
    _lock: _threading_mod.RLock
    _queue: _queue_mod.Queue[str]
    _jobs: dict[str, JobRecord]
    _funcs: dict[str, Callable[[JobRecord], dict[str, Any]]]
    _parked: dict[str, dict[str, Any]]
    _requeues: dict[str, int]
    job_timeout: float
    base_dir: str

    def _set(self, job_id: str, **fields: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    def _fail(  # pragma: no cover
        self,
        job_id: str,
        job_dir: str,
        output: str,
        message: str,
        exit_code: int | None = None,
    ) -> None:
        raise NotImplementedError

    def _live_job_dir(self, job_id: str, fallback: str) -> str:  # pragma: no cover
        raise NotImplementedError

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

    @staticmethod
    def _cancel_stop_point(job_snapshot: JobRecord, result: Any) -> tuple[str, list[str]]:
        """Derive the cancel fence marker from a finished attempt.

        Returns ``(cancelled_at_phase, completed_phases)``: the phase is the
        job type (the stop-point granularity the manager owns), and completed
        phases come from step-tracking results when the worker reports them
        (e.g. granular import's ``steps_completed``). Persisted on the
        cancelled record so :meth:`assert_no_cancel_fence` can refuse
        non-force chained resubmissions that would replay applied writes.
        """
        try:
            phase = str(job_snapshot.get("job_type") or "unknown")
        except Exception:
            phase = "unknown"
        completed: list[str] = []
        try:
            if isinstance(result, dict):
                steps = result.get("steps_completed") or []
                completed = [str(s) for s in steps]
        except Exception:
            completed = []
        return phase, completed

    def _reap_orphans(self) -> None:
        """Non-blocking orphan reap (lock-guarded)."""
        self._fences.reap()

    def _wait_for_unfence(self, job_dir: str) -> bool:
        """Block until *job_dir* is unfenced or the grace period expires."""
        return bool(self._fences.wait_unfenced(job_dir, "dir"))

    def _fence_grace_secs(self) -> float:
        """Bounded fence grace for off-lane parking (delegates to tracker)."""
        try:
            return float(self._fences.grace_secs())
        except Exception:
            try:
                grace = min(float(self.job_timeout), 120.0)
            except (TypeError, ValueError):
                grace = 120.0
            return max(grace, 1.0)

    def _fence_ttl_secs(self) -> float:
        """Fence TTL for orphan holds (delegates to tracker)."""
        try:
            return float(self._fences.ttl_secs())
        except Exception:
            try:
                return max(2.0 * float(self.job_timeout), 300.0)
            except (TypeError, ValueError):
                return 300.0

    def _max_park_attempts(self) -> int:
        """Park attempts sized to outlive one fence TTL.

        A single slow target fences its pair for the full TTL; parked
        same-pair jobs must wait that long instead of burning through a
        fixed 5 short graces and failing while the fence is still held.
        Total wait = attempts * grace >= TTL + one grace buffer. The bound
        is still finite (TTL/grace + 1, e.g. 61 parks at defaults) so a
        wedged-forever orphan fails loudly after one TTL instead of
        spinning forever.
        """
        import math

        grace = self._fence_grace_secs()
        ttl = self._fence_ttl_secs()
        needed = int(math.ceil(ttl / max(grace, 1.0))) + 1
        return max(5, needed)

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
                    stable_fp = str(info.get("stable_fp") or "")
                    fence = str(info.get("fence") or "workdir")
                    until = float(info.get("until") or 0.0)
                dir_fenced = bool(work_dir) and work_dir in fenced_dirs
                pair_fenced = (bool(pair_fp) and pair_fp in fenced_pairs) or (
                    bool(stable_fp) and stable_fp in fenced_pairs
                )
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
        park bound (see :meth:`_max_park_attempts`) outlives one fence TTL
        so one slow target no longer converts healthy queued same-pair jobs
        into failures; past the bound the job fails loudly instead of
        spinning forever. Status transitions are unchanged (queued ->
        queued on park, queued -> failed on expiry) so existing fence tests
        keep passing.
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
            max_attempts = self._max_park_attempts()
            if attempts < max_attempts:
                self._requeues[job_id] = attempts + 1
                try:
                    pair_fp = str((job.get("params") or {}).get(SNAPSHOT_FP) or "")
                    stable_fp = str((job.get("params") or {}).get(SNAPSHOT_STABLE) or "")
                except Exception:
                    pair_fp = ""
                    stable_fp = ""
                self._parked[job_id] = {
                    "work_dir": work_dir,
                    "pair_fp": pair_fp,
                    "stable_fp": stable_fp,
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
                max_attempts,
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
                    stable_fp = str((job.get("params") or {}).get(SNAPSHOT_STABLE) or "")
                    # Fence membership is read outside the manager lock
                    # (tracker-owned, one snapshot); parking below is the
                    # real guard, so a fence landing here only delays the
                    # job off-lane instead of running it concurrently.
                    fenced_dirs, fenced_pairs = self._fences.fenced_snapshot()
                    dir_fenced = work_dir in fenced_dirs
                    pair_fenced = (bool(pair_fp) and pair_fp in fenced_pairs) or (
                        bool(stable_fp) and stable_fp in fenced_pairs
                    )
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
                            phase, completed = self._cancel_stop_point(job_snapshot, None)
                            self._set(
                                job_id,
                                status="cancelled",
                                error="Cancelled by operator (timed out while cancelling)",
                                cancelled_at_phase=phase,
                                completed_phases=completed,
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
                            timed_out_stable = timed_out_params.get(SNAPSHOT_STABLE)
                        self._fences.note_timeout(
                            future=future,
                            pool=pool,
                            job_dir=live_dir,
                            pair_fp=str(timed_out_fp) if timed_out_fp else None,
                            stable_fp=str(timed_out_stable) if timed_out_stable else None,
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
                        phase, completed = self._cancel_stop_point(job_snapshot, result)
                        self._set(
                            job_id,
                            status="cancelled",
                            error="Cancelled by operator (best-effort: the workflow is not "
                            "preemptive, so target writes may already be applied; "
                            "verify before resubmitting to avoid replaying them)",
                            result=_normalize_result(result),
                            cancelled_at_phase=phase,
                            completed_phases=completed,
                        )
                    else:
                        self._set(job_id, status="succeeded", result=_normalize_result(result))
                except click.exceptions.Exit as exc:
                    output = _bounded_output(buffer.getvalue())
                    code = int(exc.exit_code or 0)
                    live_dir = self._live_job_dir(job_id, job_dir)
                    with self._lock:
                        current = self._jobs.get(job_id)
                        cancelled = bool(current and current.get("cancel_requested"))
                    if cancelled:
                        # Cancel wins over exit: the operator asked to stop,
                        # so report cancelled with the fence markers (same as
                        # the normal path) instead of succeeded/failed.
                        phase, completed = self._cancel_stop_point(job_snapshot, None)
                        _persist_console(live_dir, output, job_id)
                        self._set(
                            job_id,
                            status="cancelled",
                            error="Cancelled by operator (best-effort: the workflow is not "
                            "preemptive, so target writes may already be applied; "
                            "verify before resubmitting to avoid replaying them)",
                            cancelled_at_phase=phase,
                            completed_phases=completed,
                        )
                    elif code == 0:
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
                    with self._lock:
                        current = self._jobs.get(job_id)
                        cancelled = bool(current and current.get("cancel_requested"))
                    if cancelled:
                        phase, completed = self._cancel_stop_point(job_snapshot, None)
                        live_dir = self._live_job_dir(job_id, job_dir)
                        _persist_console(live_dir, _bounded_output(buffer.getvalue()), job_id)
                        self._set(
                            job_id,
                            status="cancelled",
                            error="Cancelled by operator (best-effort: the workflow is not "
                            "preemptive, so target writes may already be applied; "
                            "verify before resubmitting to avoid replaying them)",
                            cancelled_at_phase=phase,
                            completed_phases=completed,
                        )
                    else:
                        try:
                            detail = exc.format_message()
                        except Exception:
                            detail = str(exc)
                        # Scrub before persist/serve: ClickException wraps
                        # backend errors via str(e) at CLI call sites, so
                        # token/credential bytes must not reach pollers.
                        # Console output for the same run is scrubbed.
                        message = _scrub_output(detail.strip() or "Job failed")
                        # Truncate: polled payloads carry the actionable line;
                        # full tracebacks stay server-side.
                        live_dir = self._live_job_dir(job_id, job_dir)
                        self._fail(
                            job_id, live_dir, _bounded_output(buffer.getvalue()), message[:500]
                        )
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
