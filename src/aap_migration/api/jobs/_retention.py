"""Retention and confined directory removal for API background jobs.

Single home for ``MAX_JOBS`` eviction (drop oldest terminal jobs first)
and best-effort confined ``rmtree``, so the FIFO manager module stays
queue/transition orchestration. Functions take explicit state (no hidden
manager coupling); the manager holds its lock across the calls.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any

log = logging.getLogger("aap_migration.api.jobs")


def remove_job_dir(job_dir: str, base_dir: str) -> None:
    """Best-effort confined removal of one job directory (never raises)."""
    if not job_dir or not base_dir:
        return
    try:
        base = os.path.abspath(base_dir)
        target = os.path.abspath(job_dir)
        if os.path.commonpath([target, base]) != base or target == base:
            return
        shutil.rmtree(target, ignore_errors=True)
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("job dir cleanup failed for %s: %s", job_dir, exc)


ORPHAN_QUARANTINE_DIRNAME = ".orphan-quarantine"


def sweep_orphan_dirs(
    base_dir: str,
    live_dirs: set[str],
    fenced_dirs: set[str],
    *,
    max_age_secs: float = 600.0,
    quarantine_max: int = 20,
) -> tuple[int, int]:
    """Quarantine record-less, drained job dirs (P3 #27; never raises).

    A timed-out attempt whose record was deleted or evicted leaves its
    directory behind by design (the orphan may still be writing into it),
    so per-record deletion can never reclaim it. Once the orphan drains
    (unfenced) and no live record references the directory, keeping it in
    the job tree grows disk usage until submissions start erroring.
    Quarantine moves such dirs to ``<base>/.orphan-quarantine/`` (audit
    artifacts survive the move; the orphan is done, so nothing still
    writes there) instead of deleting them.

    Only directories older than *max_age_secs* move, so a directory a
    concurrent submit just created (record not yet inserted) is never
    swept. Fenced and live dirs are never touched. The quarantine holds
    at most *quarantine_max* dirs (0 disables the sweep entirely);
    beyond the cap the oldest quarantined dirs are removed with a warning
    -- the one documented exception to never-auto-delete, bounded and
    observable. Returns (quarantined, removed) counts.
    """
    if not base_dir or quarantine_max <= 0:
        return (0, 0)
    try:
        import time as _time

        base = os.path.abspath(base_dir)
        if not os.path.isdir(base):
            return (0, 0)
        live = {os.path.abspath(d) for d in live_dirs if d}
        fenced = {os.path.abspath(d) for d in fenced_dirs if d}
        try:
            names = os.listdir(base)
        except OSError:
            return (0, 0)
        now = _time.time()
        quarantine = os.path.join(base, ORPHAN_QUARANTINE_DIRNAME)
        moved = 0
        for name in names:
            if name.startswith("."):
                continue
            full = os.path.join(base, name)
            try:
                if not os.path.isdir(full) or os.path.islink(full):
                    continue
                if full in live or full in fenced:
                    continue
                try:
                    if now - os.path.getmtime(full) < max_age_secs:
                        continue
                except OSError:
                    continue
                os.makedirs(quarantine, exist_ok=True)
                dest = os.path.join(quarantine, name)
                if os.path.lexists(dest):
                    dest = os.path.join(quarantine, f"{name}__dup_{int(now)}")
                os.rename(full, dest)
                moved += 1
            except Exception as exc:
                log.warning("orphan quarantine failed for %s: %s", full, exc)
        removed = _enforce_quarantine_cap(quarantine, quarantine_max)
        if moved or removed:
            log.warning(
                "orphan sweep: quarantined %d directorie(s), removed %d "
                "overflowed quarantined directorie(s) under %s",
                moved,
                removed,
                base,
            )
        return (moved, removed)
    except Exception as exc:  # never break submission on a sweep failure
        log.warning("orphan sweep failed for %s: %s", base_dir, exc)
        return (0, 0)


def _enforce_quarantine_cap(quarantine: str, quarantine_max: int) -> int:
    """Remove oldest quarantined dirs past the cap (best-effort)."""
    try:
        try:
            names = os.listdir(quarantine)
        except OSError:
            return 0
        entries: list[tuple[float, str]] = []
        for name in names:
            full = os.path.join(quarantine, name)
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    entries.append((os.path.getmtime(full), full))
            except OSError:
                continue
        entries.sort()
        overflow = entries[: max(0, len(entries) - max(0, quarantine_max))]
        removed = 0
        for _, full in overflow:
            try:
                shutil.rmtree(full, ignore_errors=True)
                if not os.path.exists(full):
                    removed += 1
                    log.warning(
                        "orphan quarantine overflow: removed oldest %s "
                        "(audit policy exception: drained orphans only)",
                        full,
                    )
            except Exception as exc:
                log.warning("orphan quarantine overflow cleanup failed for %s: %s", full, exc)
        return removed
    except Exception as exc:
        log.warning("orphan quarantine cap enforcement failed for %s: %s", quarantine, exc)
        return 0


def evict_locked(
    jobs: dict[str, Any],
    funcs: dict[str, Any],
    requeues: dict[str, int],
    fenced_dirs: set[str],
    max_jobs: int,
    active_statuses: tuple[str, ...],
    terminal_statuses: tuple[str, ...],
) -> tuple[str, ...]:
    """Enforce retention: drop oldest terminal jobs first.

    A directory shared with **any** surviving record (queued, running,
    or terminal -- chained phases share one dir) is never removed, so
    eviction cannot wipe artifacts out from under a queued chained
    child or a surviving sibling. Terminals referenced by a queued or
    running chained child (``params.job_id``) are never evicted, even
    when over max (mirrors the delete() guard); the next-oldest
    unreferenced terminal is evicted instead. Callers hold the manager
    lock.

    Returns confined directories safe to remove; the caller removes
    them **after** releasing the lock so synchronous ``rmtree`` never
    stalls job submission under the lock.
    """
    if len(jobs) <= max_jobs:
        return ()
    # Parent records still needed by queued/running chained children
    # must survive eviction (delete() rejects the same case with 409).
    referenced: set[str] = {
        str((other.get("params") or {}).get("job_id"))
        for other in jobs.values()
        if other.get("status") in active_statuses and (other.get("params") or {}).get("job_id")
    }
    terminal = sorted(
        (
            j
            for j in jobs.values()
            if j["status"] in terminal_statuses and j["job_id"] not in referenced
        ),
        key=lambda j: j["created_at"],
    )
    victims: list[str] = []
    while len(jobs) > max_jobs and terminal:
        oldest = terminal.pop(0)
        jid = oldest["job_id"]
        jobs.pop(jid, None)
        funcs.pop(jid, None)
        requeues.pop(jid, None)
        victims.append(str(oldest.get("job_dir", "")))
    survivors = {str(other.get("job_dir", "")) for other in jobs.values()} | set(fenced_dirs)
    return tuple(d for d in victims if d and d not in survivors)
