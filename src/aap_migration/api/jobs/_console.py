"""Per-job console capture and bounded log helpers.

Routes worker-thread writes to a buffer while leaving other threads on
the real streams, and bounds every persist/serve path so one verbose
job cannot exhaust memory or disk.
"""

from __future__ import annotations

import os
import sys
import threading
from io import StringIO
from typing import Any

from aap_migration.api.jobs._config import CONSOLE_MAX_BYTES, CONSOLE_TAIL_BYTES
from aap_migration.api.jobs._scrub import _scrub_output


class _ThreadLocalProxy:
    """Route worker-thread writes to a buffer, others to the real stream."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self._local = threading.local()

    def bind(self, buffer: StringIO) -> None:
        self._local.buffer = buffer

    def unbind(self) -> None:
        self._local.__dict__.pop("buffer", None)

    def write(self, data: str) -> int:
        buf = self._local.__dict__.get("buffer")
        if buf is not None:
            buf.write(data)
            return len(data)
        wrote = self._real.write(data)
        return wrote if isinstance(wrote, int) else len(data)

    def writelines(self, lines: Any) -> None:
        buf = self._local.__dict__.get("buffer")
        if buf is not None:
            buf.writelines(lines)
        else:
            self._real.writelines(lines)

    def flush(self) -> None:
        buf = self._local.__dict__.get("buffer")
        if buf is not None:
            buf.flush()
        else:
            self._real.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


_stdout_proxy = _ThreadLocalProxy(sys.stdout)
_stderr_proxy = _ThreadLocalProxy(sys.stderr)
if sys.stdout is not _stdout_proxy:
    sys.stdout = _stdout_proxy
if sys.stderr is not _stderr_proxy:
    sys.stderr = _stderr_proxy


def _bounded_output(output: str) -> str:
    """Truncate in-memory job output so one verbose job cannot exceed the cap."""
    if len(output) > CONSOLE_MAX_BYTES:
        keep = CONSOLE_MAX_BYTES // 2
        output = "...[truncated]\n" + output[-keep:]
    return output


def _persist_console(job_dir: str, output: str, job_id: str | None = None) -> None:
    if not job_dir:
        return
    output = _scrub_output(output)
    if len(output) > CONSOLE_MAX_BYTES:
        keep = CONSOLE_MAX_BYTES // 2
        output = "...[truncated]\n" + output[-keep:]
    for path in [os.path.join(job_dir, "console.log")] + (
        [os.path.join(job_dir, f"console-{job_id}.log")] if job_id else []
    ):
        try:
            if os.path.exists(path) and os.path.getsize(path) >= CONSOLE_MAX_BYTES:
                with open(path, "rb") as fh:
                    fh.seek(-CONSOLE_MAX_BYTES // 2, os.SEEK_END)
                    tail = fh.read()
                with open(path, "wb") as fh:
                    fh.write(b"...[truncated]\n" + tail)
            if (
                os.path.exists(path)
                and os.path.getsize(path) + len(output.encode("utf-8", errors="replace"))
                > CONSOLE_MAX_BYTES * 2
            ):
                with open(path, "rb") as fh:
                    fh.seek(-CONSOLE_MAX_BYTES // 2, os.SEEK_END)
                    tail = fh.read()
                with open(path, "wb") as fh:
                    fh.write(b"...[truncated]\n" + tail)
            with open(path, "a") as fh:
                fh.write(output)
        except OSError:
            pass


def _console_tail(output: str, lines: int = 15, max_bytes: int = 8192) -> str:
    output = _scrub_output(output)
    tail = "\n".join(output.splitlines()[-lines:])
    if len(tail.encode("utf-8", errors="replace")) > max_bytes:
        tail = tail[-max_bytes:]
        tail = "...[truncated]\n" + tail
    return tail if tail else "(no console output)"


def read_console_tail(
    job_dir: str, max_bytes: int = CONSOLE_TAIL_BYTES, job_id: str | None = None
) -> str:
    """Read the tail of a job console.log with a byte cap (no full-file load).

    Prefers the per-job ``console-<job_id>.log`` (isolated per chained
    phase) when *job_id* is given and that file exists; falls back to the
    shared ``console.log``.
    """
    candidates = ([os.path.join(job_dir, f"console-{job_id}.log")] if job_id else []) + [
        os.path.join(job_dir, "console.log")
    ]
    path = candidates[0]
    try:
        size = os.path.getsize(path)
    except OSError:
        if len(candidates) > 1:
            path = candidates[1]
            try:
                size = os.path.getsize(path)
            except OSError:
                return ""
        else:
            return ""
    try:
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
            data = fh.read()
        return _scrub_output(data.decode("utf-8", errors="replace"))
    except OSError:
        return ""
