"""Restart-durable terminal job index for API background jobs.

In-memory queue state is not durable across restarts: a restart drops all
records and fences, and queued work must be resubmitted. To keep restarts
actionable instead of bare 404s, every terminal transition appends a
summary line to ``<base_dir>/.job_index.jsonl`` (bounded); unknown ids
found there report "submitted before the last restart; resubmit to rerun".
Orphaned job directories from a previous run are counted and logged at
startup for observability (never auto-deleted: they may hold audit
artifacts).
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("aap_migration.api.jobs")


class RestartIndex:
    """Bounded on-disk index of terminal job transitions (best-effort).

    All methods never raise: persistence must not fail job transitions.
    ``max_jobs`` bounds retained summaries; pass the live config value.
    """

    def __init__(self, base_dir: str, max_jobs: int = 1000) -> None:
        self._base_dir = base_dir
        self._max_jobs = max(1, int(max_jobs))

    @property
    def path(self) -> str:
        """Absolute path of the index file."""
        return os.path.join(self._base_dir, ".job_index.jsonl")

    def record_terminal(self, job_id: str, job_type: str, status: str, updated_at: str) -> None:
        """Append a terminal summary (best-effort, never raises)."""
        try:
            with open(self.path, "a") as fh:
                fh.write(
                    json.dumps(
                        {
                            "job_id": job_id,
                            "job_type": job_type,
                            "status": status,
                            "updated_at": updated_at,
                        }
                    )
                    + "\n"
                )
            try:
                if os.path.getsize(self.path) > 1 << 20:
                    self.compact()
            except OSError:
                pass
        except OSError:
            pass

    def compact(self) -> None:
        """Keep the newest summaries (bounded retention, never raises)."""
        try:
            with open(self.path) as fh:
                lines = fh.readlines()
            keep = lines[-self._max_jobs :]
            with open(self.path, "w") as fh:
                fh.writelines(keep)
        except OSError:
            pass

    def unknown_message(self, job_id: str) -> str:
        """Actionable unknown-id error: name a pre-restart id as resubmittable."""
        try:
            with open(self.path) as fh:
                for line in fh:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("job_id") == job_id:
                        return (
                            f"Job '{job_id}' was submitted before the last restart "
                            f"(was {entry.get('status')}); resubmit to rerun it."
                        )
        except OSError:
            pass
        return f"Job '{job_id}' not found"

    def log_stale_dirs(self) -> None:
        """Count orphaned job dirs from a previous run (observability only)."""
        try:
            entries = [
                name
                for name in os.listdir(self._base_dir)
                if os.path.isdir(os.path.join(self._base_dir, name)) and not name.startswith(".")
            ]
        except OSError:
            return
        if entries:
            log.warning(
                "job base dir holds %d directorie(s) from a previous run with no "
                "live records (restart drops in-memory state); they are kept "
                "for audit, resubmit in-flight work",
                len(entries),
            )
