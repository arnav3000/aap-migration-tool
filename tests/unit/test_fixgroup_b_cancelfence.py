"""Unit tests for P1 #3: cancel fence on the FIFO job manager.

Cancel of a running job only sets a flag; the pool thread finishes the phase
and records cancelled-with-result. Chained resume/retry/resubmit onto such a
job must require explicit ``force=true`` (refused with 409 otherwise).
"""

import uuid
from typing import Any

import pytest

from aap_migration.api.jobs._records import ConflictError
from aap_migration.api.jobs.manager import JobManager


def _make_manager(tmp_path: Any) -> JobManager:
    return JobManager(base_dir=str(tmp_path / "jobs"))


def _insert_record(mgr: Any, status: str, **extra: Any) -> str:
    from aap_migration.api.jobs._records import _utcnow

    jid = f"ref-{uuid.uuid4().hex[:8]}"
    now = _utcnow()
    record = {
        "job_id": jid,
        "job_type": "migrate",
        "status": status,
        "params": {},
        "job_dir": str(jid),
        "result": None,
        "error": None,
        "error_id": None,
        "exit_code": None,
        "created_at": now,
        "updated_at": now,
        "cancel_requested": False,
    }
    record.update(extra)
    with mgr._lock:
        mgr._jobs[jid] = record
    return jid


def _submit_chained(mgr: Any, ref_id: str, force: bool = False) -> Any:
    params: dict[str, Any] = {"job_id": ref_id}
    if force:
        params["force"] = True
    return mgr.submit("import", params, lambda job: {"message": "ok"})


def test_queued_cancel_has_no_fence(tmp_path: Any) -> None:
    """A job cancelled while queued did no writes: chaining stays allowed."""
    mgr = _make_manager(tmp_path)
    ref = _insert_record(mgr, "queued")
    mgr.cancel(ref)  # queued -> cancelled immediately, no phase marker
    with mgr._lock:
        assert mgr._jobs[ref]["status"] == "cancelled"
        assert "cancelled_at_phase" not in mgr._jobs[ref]
    job = _submit_chained(mgr, ref, force=False)
    assert job["status"] == "queued"


def test_midphase_cancel_fenced_without_force(tmp_path: Any) -> None:
    """Chained resubmit onto a mid-phase-cancelled job is refused (409)."""
    mgr = _make_manager(tmp_path)
    ref = _insert_record(
        mgr,
        "cancelled",
        cancelled_at_phase="migrate",
        completed_phases=["organizations"],
        cancel_requested=True,
    )
    with pytest.raises(ConflictError, match="cancelled mid-phase"):
        _submit_chained(mgr, ref, force=False)


def test_midphase_cancel_allowed_with_force(tmp_path: Any) -> None:
    """Explicit force=true acknowledges the replay risk and submits."""
    mgr = _make_manager(tmp_path)
    ref = _insert_record(
        mgr,
        "cancelled",
        cancelled_at_phase="granular-import",
        completed_phases=["organizations", "users"],
        cancel_requested=True,
    )
    job = _submit_chained(mgr, ref, force=True)
    assert job["status"] == "queued"


def test_fence_ignores_succeeded_failed_and_unknown(tmp_path: Any) -> None:
    mgr = _make_manager(tmp_path)
    ok_ref = _insert_record(mgr, "succeeded")
    fail_ref = _insert_record(mgr, "failed")
    assert _submit_chained(mgr, ok_ref)["status"] == "queued"
    assert _submit_chained(mgr, fail_ref)["status"] == "queued"
    # Unknown ids are left for the caller's 404 path, not the fence.
    mgr.assert_no_cancel_fence("does-not-exist", force=False)
    # Unchained submits never consult the fence.
    mgr.assert_no_cancel_fence(None, force=False)


def test_cancel_stop_point_extracts_steps() -> None:
    snap: Any = {"job_id": "j", "job_type": "granular-import"}
    phase, completed = JobManager._cancel_stop_point(
        snap, {"steps_completed": ["organizations", "users"]}
    )
    assert phase == "granular-import"
    assert completed == ["organizations", "users"]


def test_cancel_stop_point_defaults() -> None:
    empty: Any = {}
    none_result: Any = None
    phase, completed = JobManager._cancel_stop_point(empty, none_result)
    assert phase == "unknown"
    assert completed == []


def test_cancel_wins_over_exit_zero(tmp_path: Any) -> None:
    """A cancel landing before click Exit(0) reports cancelled, not succeeded.

    The Exit path must honor cancel_requested like the normal path, with
    the fence markers, so chained resume/retry stays fenced (P2 #7).
    """
    import threading
    import time

    import click

    mgr = _make_manager(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def _exiter(job: Any) -> Any:
        entered.set()
        assert release.wait(timeout=30)
        raise click.exceptions.Exit(0)

    rec = mgr.submit("migrate", {}, _exiter)
    try:
        assert entered.wait(timeout=30)
        mgr.cancel(rec["job_id"])
    finally:
        release.set()
    deadline = time.monotonic() + 30
    while True:
        status = mgr.get(rec["job_id"])["status"]
        if status in ("cancelled", "succeeded", "failed"):
            break
        assert time.monotonic() < deadline, "worker did not settle"
        time.sleep(0.05)
    final: Any = mgr.get_internal(rec["job_id"])
    assert final["status"] == "cancelled"
    assert final["cancelled_at_phase"] == "migrate"
    with pytest.raises(ConflictError, match="cancelled mid-phase"):
        _submit_chained(mgr, rec["job_id"], force=False)
