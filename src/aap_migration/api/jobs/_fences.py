"""Orphan-fence tracking for the single FIFO API job worker.

A timed-out pool thread cannot be preempted and keeps applying writes: the
worker must not start overlapping work on a fenced directory (or the same
AAP pair) while the orphan is still alive. Fences are workdir paths with a
live orphan, plus pair fingerprints with a live orphan.

Lock discipline: this tracker owns its own lock and never calls back into
the manager while holding it. The manager holds its own lock while calling
in here (ordering manager -> tracker, consistently), so submit/dequeue
checks stay deadlock-free. The dequeue gate in the worker is the real
concurrency guard; the submit gate is a load-shedding fast path (a fence
landing between submit check and insert only delays the job to the dequeue
wait, never runs it concurrently).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("aap_migration.api.jobs")


def daemon_thread_pool(max_workers: int = 1, thread_name_prefix: str = "daemon-pool") -> Any:
    """Create a ThreadPoolExecutor whose worker threads are daemon threads.

    ``concurrent.futures.ThreadPoolExecutor`` spawns non-daemon threads by
    default, so a hung DNS lookup or timed-out job thread blocks interpreter
    exit and leaks the pool. This helper patches the pool's thread creation
    so every worker is ``daemon=True`` (never blocks exit); pools must still
    be shut down explicitly, but a leaked hung thread can no longer wedge
    the process. Shared home for the fence-orphan pools (manager), the
    bounded SSRF DNS pool, and any other helper-thread pool.
    """
    import concurrent.futures
    import threading

    pool: Any = concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix=thread_name_prefix
    )
    _orig_adjust = pool._adjust_thread_count

    def _daemon_adjust() -> None:
        _orig_thread = threading.Thread

        class _DaemonThread(_orig_thread):  # type: ignore[valid-type,misc]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["daemon"] = True
                super().__init__(*args, **kwargs)

        threading.Thread = _DaemonThread  # type: ignore[misc]
        try:
            _orig_adjust()
        finally:
            threading.Thread = _orig_thread  # type: ignore[misc]

    pool._adjust_thread_count = _daemon_adjust
    return pool


class FenceTracker:
    """Live-orphan set plus derived directory/pair fences (thread-safe).

    Pair fencing uses two keys: the credential-mixing fingerprint (for
    drift detection) and a stable URL-only key (for fencing). Both are
    stored in the union ``_fenced_pairs`` so a token rotation or TLS edit
    to the same physical controllers stays fenced until orphans drain,
    while a URL retarget to different controllers correctly unfences.
    A third set, ``_fenced_targets``, holds the target-side-only stable
    key (P1 #4): the full-pair keys mix both endpoints, so a resubmission
    from a different source to the same target would otherwise slip both
    gates while a timed-out orphan is still writing to that target.
    Source-only jobs carry no target key and never block on it.
    """

    def __init__(
        self,
        *,
        max_orphans: Callable[[], int],
        job_timeout: Callable[[], float],
    ) -> None:
        self._max_orphans = max_orphans
        self._job_timeout = job_timeout
        self._lock = threading.Lock()
        self._orphans: list[dict[str, Any]] = []
        self._fenced_dirs: set[str] = set()
        self._fenced_pairs: set[str] = set()
        self._fenced_targets: set[str] = set()

    # -- introspection (snapshots, never raise) -------------------------
    def snapshot(self) -> dict[str, int]:
        """Orphan/fence counts for readiness guards and health telemetry."""
        with self._lock:
            return {
                "orphans": len(self._orphans),
                "fenced_dirs": len(self._fenced_dirs),
                "fenced_pairs": len(self._fenced_pairs),
                "fenced_targets": len(self._fenced_targets),
            }

    def fenced_dirs_snapshot(self) -> set[str]:
        """Copy of currently fenced directories (eviction must not wipe them)."""
        with self._lock:
            return set(self._fenced_dirs)

    def fenced_snapshot(self) -> tuple[set[str], set[str]]:
        """Copy of (fenced dirs, fenced pairs) for dequeue gating."""
        with self._lock:
            return set(self._fenced_dirs), set(self._fenced_pairs)

    def fenced_targets_snapshot(self) -> set[str]:
        """Copy of fenced target-side keys for dequeue gating (P1 #4)."""
        with self._lock:
            return set(self._fenced_targets)

    def is_fenced_dir(self, job_dir: str) -> bool:
        """True when *job_dir* currently has a live orphan (reaps first)."""
        if not job_dir:
            return False
        self.reap()
        with self._lock:
            return job_dir in self._fenced_dirs

    def is_fenced_pair(self, pair_fp: str, stable_fp: str | None = None) -> bool:
        """True when *pair_fp* or *stable_fp* has a live orphan (reaps first)."""
        if not pair_fp and not stable_fp:
            return False
        self.reap()
        with self._lock:
            if pair_fp and pair_fp in self._fenced_pairs:
                return True
            return bool(stable_fp and stable_fp in self._fenced_pairs)

    def is_fenced_target(self, target_stable: str | None) -> bool:
        """True when the target side has a live orphan (reaps first, P1 #4)."""
        if not target_stable:
            return False
        self.reap()
        with self._lock:
            return target_stable in self._fenced_targets

    # -- lifecycle -------------------------------------------------------
    def reap(self) -> None:
        """Drop finished orphans and unfence their directories/pairs.

        A timed-out pool thread cannot be preempted, so its pool is shut
        down (no-wait) only once its future reports done; the fence is held
        until then. The fence deadline is observability only: at expiry a
        warning names the runaway, but the fence stays until the thread
        actually finishes -- releasing it early would let the next dequeue
        start overlapping writes on the same dir/pair (state corruption).
        A wedged-forever thread bricks its pair until restart; parked jobs
        fail loudly with resubmit-after-drain instead of running concurrently.
        """
        with self._lock:
            now = time.monotonic()
            live: list[dict[str, Any]] = []
            for orphan in self._orphans:
                future = orphan["future"]
                if future.done():
                    try:
                        orphan["pool"].shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass
                    continue
                expires_at = orphan.get("fence_expires_at")
                if expires_at is not None and now >= float(expires_at):
                    if not orphan.get("fence_expired"):
                        log.warning(
                            "orphan fence deadline passed for dir=%r pair=%r; "
                            "holding fence until the runaway thread finishes "
                            "(parked jobs fail loudly instead of overlapping)",
                            orphan.get("job_dir"),
                            orphan.get("pair_fp"),
                        )
                        orphan["fence_expired"] = True
                live.append(orphan)
            self._orphans = live
            self._fenced_dirs = {str(o["job_dir"]) for o in live if o.get("job_dir")}
            pairs: set[str] = set()
            for o in live:
                if o.get("pair_fp"):
                    pairs.add(str(o["pair_fp"]))
                if o.get("stable_fp"):
                    pairs.add(str(o["stable_fp"]))
            self._fenced_pairs = pairs
            self._fenced_targets = {str(o["target_stable"]) for o in live if o.get("target_stable")}

    def check_submit(
        self,
        work_dir: str,
        pair_fp: str,
        stable_fp: str | None = None,
        target_stable: str | None = None,
    ) -> str | None:
        """Load-shedding gate for new submissions (reaps first).

        Returns an error message when the submission must shed load
        (orphan cap reached or target fenced), else None. Checks both the
        credential-mixing fingerprint and the stable URL-only key so a
        token rotation to the same controllers stays fenced, plus the
        target-side key so a different-source resubmission to a fenced
        target sheds too (P1 #4).
        """
        self.reap()
        with self._lock:
            if len(self._orphans) >= self._max_orphans():
                return f"Too many timed-out jobs draining ({len(self._orphans)}); retry later"
            if work_dir in self._fenced_dirs:
                return (
                    "Workdir fenced by a timed-out attempt still running; resubmit after it drains"
                )
            if pair_fp and pair_fp in self._fenced_pairs:
                return (
                    "AAP pair fenced by a timed-out attempt still running; resubmit after it drains"
                )
            if stable_fp and stable_fp in self._fenced_pairs:
                return (
                    "AAP pair fenced by a timed-out attempt still running; resubmit after it drains"
                )
            if target_stable and target_stable in self._fenced_targets:
                return "AAP target fenced by a timed-out attempt still running; resubmit after it drains"
        return None

    def grace_secs(self) -> float:
        """Bounded fence grace for off-lane parking (single home).

        Mirrors the dequeue wait bound so park sizing and wait_unfenced
        stay in lockstep when bounds are next tuned.
        """
        try:
            grace = min(float(self._job_timeout()), 120.0)
        except (TypeError, ValueError):
            grace = 120.0
        return max(grace, 1.0)

    def ttl_secs(self) -> float:
        """Fence TTL for orphan holds (single home)."""
        try:
            return max(2.0 * float(self._job_timeout()), 300.0)
        except (TypeError, ValueError):
            return 300.0

    def wait_unfenced(self, key: str, kind: str) -> bool:
        """Block until *key* is unfenced or the grace period expires.

        ``kind`` is ``"dir"`` or ``"pair"``. Grace is at most the job
        timeout, capped at 2 minutes, so a hung orphan fails the gated job
        loudly instead of wedging the single FIFO worker forever. When
        orphan pressure is at cap, fail fast without burning worker time.
        Returns True when clear to run.
        """
        if not key:
            return True
        if kind not in ("dir", "pair"):
            raise ValueError(f"kind must be 'dir' or 'pair', got {kind!r}")
        with self._lock:
            if len(self._orphans) >= self._max_orphans():
                return False
        grace = self.grace_secs()
        deadline = time.monotonic() + max(grace, 1.0)
        while True:
            self.reap()
            with self._lock:
                fenced_set = self._fenced_dirs if kind == "dir" else self._fenced_pairs
                fenced = key in fenced_set
            if not fenced:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)

    def note_timeout(
        self,
        *,
        future: Any,
        pool: Any,
        job_dir: str,
        pair_fp: str | None,
        stable_fp: str | None = None,
        target_stable: str | None = None,
    ) -> None:
        """Fence a timed-out attempt's directory and pair (never raises)."""
        try:
            fence_ttl = self.ttl_secs()
            with self._lock:
                self._orphans.append(
                    {
                        "future": future,
                        "pool": pool,
                        "job_dir": job_dir,
                        "pair_fp": pair_fp,
                        "stable_fp": stable_fp,
                        "target_stable": target_stable,
                        "fence_expires_at": time.monotonic() + max(fence_ttl, 1.0),
                        "fence_expired": False,
                    }
                )
                if job_dir:
                    self._fenced_dirs.add(job_dir)
                if pair_fp:
                    self._fenced_pairs.add(str(pair_fp))
                if stable_fp:
                    self._fenced_pairs.add(str(stable_fp))
                if target_stable:
                    self._fenced_targets.add(str(target_stable))
                # Defensive bound: drop done futures only so the list cannot
                # grow past 2x cap on timeout bursts. A hung (not-done)
                # orphan's fence is never released on overflow: its daemon
                # thread may still be applying writes, and dropping it
                # would let the next dequeue start overlapping work on the
                # same dir/pair. Over-cap hung pressure sheds via
                # check_submit (429) and wait_unfenced (park/fail) instead.
                cap = self._max_orphans()
                if len(self._orphans) > cap * 2:
                    done_idx = [i for i, o in enumerate(self._orphans) if o["future"].done()]
                    while len(self._orphans) > cap * 2 and done_idx:
                        i = done_idx.pop(0)
                        orphan = self._orphans.pop(i)
                        try:
                            orphan["pool"].shutdown(wait=False, cancel_futures=True)
                        except Exception:
                            pass
                        done_idx = [j - 1 for j in done_idx if j - 1 >= 0]
                    if len(self._orphans) > cap * 2:
                        log.error(
                            "orphan fence cap exceeded (%d > 2*%d) with all "
                            "futures hung; retaining every fence and shedding "
                            "new fence-gated work until orphans drain",
                            len(self._orphans),
                            cap,
                        )
                    # Rebuild fence sets from the retained orphans, keeping
                    # deadline-expired runaways fenced (reap() holds every
                    # live fence until its thread finishes) so no live fence
                    # is released early. Both fp and stable keys are retained.
                    self._fenced_dirs = {
                        str(o["job_dir"]) for o in self._orphans if o.get("job_dir")
                    }
                    pairs: set[str] = set()
                    for o in self._orphans:
                        if o.get("pair_fp"):
                            pairs.add(str(o["pair_fp"]))
                        if o.get("stable_fp"):
                            pairs.add(str(o["stable_fp"]))
                    self._fenced_pairs = pairs
                    self._fenced_targets = {
                        str(o["target_stable"]) for o in self._orphans if o.get("target_stable")
                    }
        except Exception:
            log.exception("orphan fence bookkeeping failed")
