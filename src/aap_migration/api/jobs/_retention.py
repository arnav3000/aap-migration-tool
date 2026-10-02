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
