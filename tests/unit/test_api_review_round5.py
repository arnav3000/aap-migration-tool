"""Round-5 review fixes: worker lifecycle pins, posture veto, cap accounting.

Covers the actionable test findings from the round-5 full review
(PR #127): rotation-guard pinning (#5), cancel/timeout branch (#7),
park-bound sizing invariant (#33), pair-fence branch (#15), Exit(0)
branch (#16), BaseException survival (#18), TLS-weakening veto (#3),
IntegrityError envelope (#11), WorkdirGoneError mapping (#20), parked
cap exclusion (#9), shutdown-drain verdicts (#19), and the singleflight
degraded probe (#8).
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import Mock

import click
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from aap_migration.api.jobs import get_job_manager
from aap_migration.api.jobs._fences import FenceTracker
from aap_migration.api.jobs._records import WorkdirGoneError
from aap_migration.api.routers._common import _store_http_error
from aap_migration.api.store import (
    SNAPSHOT_FERNET_FP,
    SNAPSHOT_NEED,
)


def _terminal(manager: Any, job_id: str, timeout: int = 30) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.get(job_id)
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise TimeoutError(f"job {job_id} not terminal in {timeout}s")


# -- P1 #5: Fernet rotation fail-fast branch ---------------------------------
def test_fernet_rotation_guard_fails_fast(client: TestClient, monkeypatch: Any) -> None:
    """A rotation between submit and execution fails with the drain message."""
    from cryptography.fernet import Fernet

    from aap_migration.api.context import verify_execution_pair
    from aap_migration.api.security import fernet_key_fingerprint

    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()
    monkeypatch.setenv("AAP_BRIDGE_API_KEY", key_a)
    fp_a = fernet_key_fingerprint()
    assert fp_a
    monkeypatch.setenv("AAP_BRIDGE_API_KEY", key_b)
    stale = {SNAPSHOT_NEED: "none", SNAPSHOT_FERNET_FP: "stale-fingerprint"}
    with pytest.raises(ValueError, match="drain the queue"):
        verify_execution_pair(stale)
    # Control: a matching fingerprint passes the rotation guard and falls
    # through to normal pair resolution (which fails here for missing
    # connections -- proving the guard itself passed).
    current = {SNAPSHOT_NEED: "none", SNAPSHOT_FERNET_FP: fernet_key_fingerprint()}
    with pytest.raises(ValueError, match="No source AAP configured"):
        verify_execution_pair(current)


# -- P1 #7: timeout-while-cancelling branch ------------------------------------
def test_timeout_while_cancelling_reports_cancelled(client: TestClient) -> None:
    """A job cancelled while its attempt runs past timeout reports cancelled."""
    import time as _time

    manager = get_job_manager()
    manager.job_timeout = 0.5
    started = threading.Event()

    def _slow(job: Any) -> Any:
        started.set()
        _time.sleep(3)
        return {"message": "too late"}

    rec = manager.submit("slow-cancel", {}, _slow)
    assert started.wait(timeout=30)
    cancelled = manager.cancel(rec["job_id"])
    assert cancelled["status"] == "running"
    final = _terminal(manager, rec["job_id"])
    assert final["status"] == "cancelled", final
    internal = manager.get_internal(rec["job_id"])
    assert internal.get("cancelled_at_phase"), internal


# -- P2 #33: park-bound production sizing --------------------------------------
def test_park_bound_outlives_fence_ttl(client: TestClient) -> None:
    """Park attempts * grace must cover one fence TTL plus one grace."""
    manager = get_job_manager()
    for timeout in (60.0, 300.0, 3600.0, 7200.0):
        manager.job_timeout = timeout
        grace = manager._fence_grace_secs()
        ttl = manager._fence_ttl_secs()
        attempts = manager._max_park_attempts()
        assert attempts >= 5
        assert attempts * grace >= ttl + grace


# -- P2 #15: pair-kind fence wait branch ----------------------------------------
def test_fence_pair_kind_wait_and_invalid_kind() -> None:
    """kind='pair' consults pair fences; an invalid kind raises ValueError."""
    tracker = FenceTracker(max_orphans=lambda: 10, job_timeout=lambda: 60)
    assert tracker.wait_unfenced("anything", "dir") is True
    with pytest.raises(ValueError, match="kind must be"):
        tracker.wait_unfenced("x", "bogus")
    # A done orphan reaps immediately: the pair reads clear.
    done_future = Mock()
    done_future.done.return_value = True
    tracker.note_timeout(future=done_future, pool=Mock(), job_dir="d", pair_fp="p", stable_fp="s")
    assert tracker.wait_unfenced("p", "pair") is True
    # A live orphan holds the pair past a short grace.
    live = FenceTracker(max_orphans=lambda: 10, job_timeout=lambda: 1)
    hung = Mock()
    hung.done.return_value = False
    live.note_timeout(future=hung, pool=Mock(), job_dir="d", pair_fp="p", stable_fp="s")
    assert live.wait_unfenced("p", "pair") is False


# -- P2 #16: Click Exit(0) success branch ----------------------------------------
def test_click_exit_zero_succeeds(client: TestClient) -> None:
    """A worker func raising click Exit(0) reports succeeded with exit 0."""
    manager = get_job_manager()

    def _exit0(job: Any) -> Any:
        raise click.exceptions.Exit(0)

    rec = manager.submit("exit0", {}, _exit0)
    final = _terminal(manager, rec["job_id"])
    assert final["status"] == "succeeded", final
    assert final["exit_code"] == 0


# -- P2 #18: BaseException survival branch ---------------------------------------
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_worker_base_exception_marks_failed_and_survives(
    client: TestClient,
) -> None:
    """KeyboardInterrupt fails the job with an error id; the lane revives.

    The re-raise intentionally kills the worker thread (the supervisor
    restarts it), which pytest reports as an unhandled thread exception:
    that warning is the behavior under test, not noise.
    """
    manager = get_job_manager()

    def _boom(job: Any) -> Any:
        raise KeyboardInterrupt("stop")

    rec = manager.submit("ki", {}, _boom)
    final = _terminal(manager, rec["job_id"])
    assert final["status"] == "failed", final
    assert "error_id=" in (final["error"] or ""), final
    # The re-raise killed the worker thread; the next submit revives it.
    rec2 = manager.submit("ok", {}, lambda j: {"message": "ok"})
    final2 = _terminal(manager, rec2["job_id"])
    assert final2["status"] == "succeeded", final2


# -- P1 #3: TLS-weakening veto ----------------------------------------------------
def test_tls_weakening_override_rejected_at_submit(client: TestClient, monkeypatch: Any) -> None:
    """verify_ssl=false against a stored verify_ssl=true connection is 400."""
    from api_shared import _fake_success

    _fake_success(monkeypatch, "run_iam_audit")
    src = client.post(
        "/api/v1/connections",
        json={
            "name": "tls-src",
            "kind": "source",
            "url": "https://tls.example.com/api/v2",
            "token": "s3cret",
            "verify_ssl": True,
        },
    ).json()
    resp = client.post("/api/v1/iam/audit", json={"source_id": src["id"], "verify_ssl": False})
    assert resp.status_code == 400, resp.text
    assert "weaken" in resp.json()["detail"]
    # Strengthening (or matching) stays allowed: submits 202.
    ok = client.post("/api/v1/iam/audit", json={"source_id": src["id"], "verify_ssl": True})
    assert ok.status_code == 202, ok.text


def test_tls_weakening_override_fails_at_execution(client: TestClient, monkeypatch: Any) -> None:
    """Params bypassing submit still fail the execution-time veto."""
    from aap_migration.api.context import submit_pair_snapshot, verify_execution_pair

    src = client.post(
        "/api/v1/connections",
        json={
            "name": "tls-src2",
            "kind": "source",
            "url": "https://tls2.example.com/api/v2",
            "token": "s3cret",
            "verify_ssl": True,
        },
    ).json()
    snap = submit_pair_snapshot(src["id"], None, "source")
    # Real submit shape carries the request-level selectors alongside the
    # snapshot pins (the veto resolves through them).
    params = {"source_id": src["id"], **snap}
    params["verify_ssl"] = False
    with pytest.raises(ValueError, match="weaken"):
        verify_execution_pair(params)


# -- P1 #11: IntegrityError envelope ----------------------------------------------
def test_integrity_error_maps_to_409() -> None:
    """A unique race surfaces as 409, never an unhandled 500."""
    resp = _store_http_error(IntegrityError("stmt", "params", Exception("dup")))
    assert resp.status_code == 409


# -- P2 #20: WorkdirGoneError mapping ----------------------------------------------
def test_workdir_gone_maps_to_404() -> None:
    """A missing chained workdir is a 404 typed by exception, not message."""
    resp = _store_http_error(WorkdirGoneError("Job 'x' working directory no longer exists"))
    assert resp.status_code == 404


def test_chain_onto_missing_workdir_is_404(client: TestClient, monkeypatch: Any, pair: Any) -> None:
    """Chaining onto a job whose directory was removed reports 404."""
    import shutil

    from api_shared import _fake_success, _wait

    _fake_success(monkeypatch, "run_export")
    jid = client.post("/api/v1/exports", json={}).json()["job_id"]
    assert _wait(client, jid)["status"] == "succeeded"
    shutil.rmtree(get_job_manager().get_internal(jid)["job_dir"], ignore_errors=True)
    resp = client.post("/api/v1/exports", json={"job_id": jid})
    assert resp.status_code == 404, resp.text


# -- P1 #9: parked waiters excluded from the admission cap ---------------------------
def test_parked_waiters_excluded_from_admission_cap(client: TestClient) -> None:
    """Off-lane parked jobs must not consume the shared queue cap."""
    from typing import cast

    from aap_migration.api.jobs import JobRecord

    manager = get_job_manager()
    queued_a: JobRecord = {
        "job_id": "queued-a",
        "job_type": "t",
        "status": "queued",
        "params": {},
        "job_dir": "/tmp/qa",
        "result": None,
        "error": None,
        "error_id": None,
        "exit_code": None,
        "created_at": "now",
        "updated_at": "now",
        "cancel_requested": False,
    }
    with manager._lock:
        manager._jobs["queued-a"] = queued_a
        manager._jobs["queued-b"] = cast(
            JobRecord, dict(queued_a, job_id="queued-b", job_dir="/tmp/qb")
        )
        # Both queued: full count is 2 ...
        assert manager._admission_count_locked() == 2
        # ... but a parked waiter does not consume admission.
        manager._fences._fenced_dirs.add("/tmp/qb")
        manager._parked["queued-b"] = {
            "work_dir": "/tmp/qb",
            "pair_fp": "",
            "stable_fp": "",
            "fence": "workdir",
            "until": time.monotonic() + 600,
        }
        assert manager._admission_count_locked() == 1
        manager._parked.pop("queued-b", None)
        manager._fences._fenced_dirs.discard("/tmp/qb")
        manager._jobs.pop("queued-a", None)
        manager._jobs.pop("queued-b", None)


# -- P2 #19: shutdown drain marks leftovers actionable -------------------------------
def test_shutdown_drain_marks_leftovers_and_holds_verdict(
    client: TestClient,
) -> None:
    """Queued leftovers cancel, running leftovers fail, and the verdict sticks."""
    manager = get_job_manager()
    entered = threading.Event()
    release = threading.Event()

    def _blocker(job: Any) -> Any:
        entered.set()
        assert release.wait(timeout=30)
        return {"message": "blocker done"}

    running = manager.submit("blocker", {}, _blocker)
    assert entered.wait(timeout=30)
    queued = manager.submit("queued", {}, lambda j: {"message": "ok"})
    try:
        leftovers = manager.shutdown_drain(timeout_secs=0.5)
        assert leftovers["running"] >= 1
        cancelled = manager.get(queued["job_id"])
        assert cancelled["status"] == "cancelled", cancelled
        assert "Interrupted by server shutdown" in (cancelled["error"] or "")
        failed = manager.get(running["job_id"])
        assert failed["status"] == "failed", failed
        assert "Interrupted by server shutdown" in (failed["error"] or "")
    finally:
        release.set()
    # The pool thread finishes afterwards: the interrupted verdict must
    # not be overwritten by the normal succeeded transition.
    deadline = time.time() + 30
    while time.time() < deadline:
        current = manager.get(running["job_id"])
        if current["status"] == "failed" and "Interrupted by server shutdown" in (
            current["error"] or ""
        ):
            break
        time.sleep(0.05)
    assert "Interrupted by server shutdown" in (current["error"] or ""), current


# -- P1 #8: singleflight degraded probe ----------------------------------------------
def test_degraded_probe_singleflight(client: TestClient, monkeypatch: Any) -> None:
    """Concurrent submits while degraded share one storage probe."""
    import aap_migration.api.jobs._config as config_mod

    manager = get_job_manager()
    calls = {"n": 0}
    ready = threading.Barrier(6)

    def _slow_probe() -> bool:
        calls["n"] += 1
        time.sleep(1.0)
        config_mod.set_startup_degraded(None)
        return True

    monkeypatch.setattr(config_mod, "clear_startup_degraded_if_recovered", _slow_probe)
    config_mod.set_startup_degraded("api-db: boom (test)")
    try:
        results: dict[int, Any] = {}

        def _call(i: int) -> None:
            ready.wait(timeout=30)
            try:
                results[i] = manager._clear_degraded_bounded(timeout_secs=10)
            except Exception as exc:  # pragma: no cover - diagnostics
                results[i] = exc

        threads = [threading.Thread(target=_call, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        ready.wait(timeout=30)
        for t in threads:
            t.join(timeout=30)
        assert all(r is True for r in results.values()), results
        assert calls["n"] == 1, calls
    finally:
        config_mod.set_startup_degraded(None)
