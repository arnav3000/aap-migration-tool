"""Unit tests for P2 #10: bounded submit-time degraded re-probe.

``clear_startup_degraded_if_recovered()`` does blocking FS/DB I/O with no
timeout of its own; the manager must bound it (~5s) so a hung probe cannot
stall sync-route submit threads. On timeout the degraded state is kept and
the submission fails fast instead of hanging.
"""

import time
from typing import Any

from aap_migration.api.jobs import _config as _job_config
from aap_migration.api.jobs._records import QueueFullError
from aap_migration.api.jobs.manager import JobManager


def _make_manager(tmp_path: Any) -> JobManager:
    return JobManager(base_dir=str(tmp_path / "jobs"))


def test_hung_probe_returns_false_within_timeout(tmp_path: Any, monkeypatch: Any) -> None:
    """A hanging probe is abandoned after the timeout (returns False)."""

    def hang() -> None:
        time.sleep(30)

    monkeypatch.setattr(_job_config, "clear_startup_degraded_if_recovered", hang)
    mgr = _make_manager(tmp_path)
    begin = time.monotonic()
    assert mgr._clear_degraded_bounded(timeout_secs=0.2) is False
    assert time.monotonic() - begin < 10


def test_fast_probe_result_passes_through(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(_job_config, "clear_startup_degraded_if_recovered", lambda: True)
    assert _make_manager(tmp_path)._clear_degraded_bounded(timeout_secs=5) is True
    monkeypatch.setattr(_job_config, "clear_startup_degraded_if_recovered", lambda: False)
    assert _make_manager(tmp_path)._clear_degraded_bounded(timeout_secs=5) is False


def test_probe_error_propagates_for_caller_fallback(tmp_path: Any, monkeypatch: Any) -> None:
    def boom() -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(_job_config, "clear_startup_degraded_if_recovered", boom)
    try:
        _make_manager(tmp_path)._clear_degraded_bounded(timeout_secs=5)
    except OSError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected probe error to propagate")


def test_submit_with_hung_probe_fails_fast(tmp_path: Any, monkeypatch: Any) -> None:
    """Submit serves a 503 (QueueFullError) instead of hanging on the probe."""

    def hang() -> bool:
        time.sleep(30)
        return True

    monkeypatch.setattr(_job_config, "clear_startup_degraded_if_recovered", hang)
    _job_config.set_startup_degraded("api-db: probe me")
    try:
        mgr = _make_manager(tmp_path)
        begin = time.monotonic()
        try:
            # Short probe timeout so this test stays fast.
            monkeypatch.setenv("AAP_BRIDGE_DEGRADED_PROBE_SECS", "0.2")
            mgr.submit("migrate", {}, lambda job: {"message": "ok"})
        except QueueFullError as exc:
            assert "Server storage unhealthy" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected QueueFullError")
        assert time.monotonic() - begin < 15
    finally:
        _job_config.set_startup_degraded(None)
