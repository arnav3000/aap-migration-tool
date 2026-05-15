import asyncio
import threading


class JobService:
    def __init__(self) -> None:
        self._log_buffers: dict[str, list[str]] = {}
        self._tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._statuses: dict[str, dict] = {}
        self._lock = threading.Lock()

    def register_job(self, job_id: str) -> None:
        with self._lock:
            self._log_buffers[job_id] = []
            self._statuses[job_id] = {"status": "running"}

    def append_log(self, job_id: str, line: str) -> None:
        with self._lock:
            if job_id in self._log_buffers:
                self._log_buffers[job_id].append(line)

    def get_logs_since(self, job_id: str, offset: int) -> list[str]:
        with self._lock:
            buf = self._log_buffers.get(job_id, [])
            return buf[offset:]

    def get_job_status(self, job_id: str) -> dict | None:
        with self._lock:
            return self._statuses.get(job_id)

    def mark_completed(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._statuses:
                self._statuses[job_id] = {"status": "completed"}
            self._tasks.pop(job_id, None)

    def mark_failed(self, job_id: str, error: str) -> None:
        with self._lock:
            if job_id in self._statuses:
                self._statuses[job_id] = {"status": "failed", "error": error}
            self._tasks.pop(job_id, None)

    def register_task(self, job_id: str, task: asyncio.Task) -> None:  # type: ignore[type-arg]
        with self._lock:
            self._tasks[job_id] = task

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(job_id)
            if not task:
                return False
            task.cancel()
            if job_id in self._statuses:
                self._statuses[job_id] = {"status": "cancelled"}
            self._tasks.pop(job_id, None)
            return True
