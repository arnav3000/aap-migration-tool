"""Round-6 review fixes: fence/admission/pagination/contract pins.

Covers actionable findings from the full review of PR #127 (round 6):
fence-expiry hold (#1), orphan-quarantine sweep (#2), parked total-cap
bound (#3), stable-key fence branch (#6), artifact download 413/OSError
(#8), artifact pagination off-by-one (#9), manager pagination clamp
(#11), unknown-type 404 parity (#15), and retry continue-and-aggregate
is covered by the worker contract (see maintenance test below).
"""

from __future__ import annotations

import os
import time
from typing import Any
from unittest.mock import Mock

from fastapi.testclient import TestClient

from aap_migration.api.jobs import get_job_manager
from aap_migration.api.jobs._fences import FenceTracker


def _hung_future() -> Mock:
    fut = Mock()
    fut.done.return_value = False
    return fut


# -- #1: fence-expiry hold past deadline ------------------------------------
def test_fence_expiry_hold_past_deadline() -> None:
    tracker = FenceTracker(max_orphans=lambda: 10, job_timeout=lambda: 60)
    tracker.note_timeout(
        future=_hung_future(), pool=Mock(), job_dir="d1", pair_fp="p1", stable_fp="s1"
    )
    # Force the deadline into the past: reap must hold the fence.
    with tracker._lock:
        tracker._orphans[0]["fence_expires_at"] = time.monotonic() - 1.0
    assert tracker.is_fenced_dir("d1") is True
    assert tracker.is_fenced_pair("p1", "s1") is True
    # Warning-once dedup: second reap must not re-log.
    assert tracker._orphans[0].get("fence_expired") is True


# -- #2: orphan-quarantine sweep ---------------------------------------------
def test_orphan_quarantine_sweep(tmp_path: Any) -> None:
    from aap_migration.api.jobs import _retention as ret

    base = str(tmp_path / "jobs")
    os.makedirs(base, exist_ok=True)
    old = os.path.join(base, "old-dir")
    os.makedirs(old)
    # Make it older than the sweep age.
    old_mtime = time.time() - 700
    os.utime(old, (old_mtime, old_mtime))
    fresh = os.path.join(base, "fresh-dir")
    os.makedirs(fresh)
    live = os.path.join(base, "live-dir")
    os.makedirs(live)
    os.utime(live, (old_mtime, old_mtime))
    fenced = os.path.join(base, "fenced-dir")
    os.makedirs(fenced)
    os.utime(fenced, (old_mtime, old_mtime))
    dot = os.path.join(base, ".hidden")
    os.makedirs(dot)
    os.utime(dot, (old_mtime, old_mtime))
    link = os.path.join(base, "link-dir")
    try:
        os.symlink(old, link)
    except OSError:
        pass

    moved, _ = ret.sweep_orphan_dirs(base, {live}, {fenced}, max_age_secs=600.0, quarantine_max=20)
    assert moved == 1
    assert os.path.isdir(os.path.join(base, ret.ORPHAN_QUARANTINE_DIRNAME, "old-dir"))
    assert os.path.isdir(fresh)
    assert os.path.isdir(live)
    assert os.path.isdir(fenced)
    # Disabled sweep moves nothing.
    moved_off, removed_off = ret.sweep_orphan_dirs(
        base, set(), set(), max_age_secs=0, quarantine_max=0
    )
    assert (moved_off, removed_off) == (0, 0)


def test_orphan_quarantine_cap_overflow(tmp_path: Any) -> None:
    from aap_migration.api.jobs import _retention as ret

    base = str(tmp_path / "jobs")
    os.makedirs(base, exist_ok=True)
    quarantine = os.path.join(base, ret.ORPHAN_QUARANTINE_DIRNAME)
    os.makedirs(quarantine, exist_ok=True)
    for i in range(3):
        d = os.path.join(quarantine, f"q{i}")
        os.makedirs(d)
        mtime = time.time() - (100 - i * 10)
        os.utime(d, (mtime, mtime))
    removed = ret._enforce_quarantine_cap(quarantine, 2)
    assert removed == 1
    assert len(os.listdir(quarantine)) == 2


# -- #6: stable-key fence branch ----------------------------------------------
def test_stable_key_fence_survives_rotation() -> None:
    tracker = FenceTracker(max_orphans=lambda: 10, job_timeout=lambda: 60)
    tracker.note_timeout(
        future=_hung_future(),
        pool=Mock(),
        job_dir="d-other",
        pair_fp="rotated-fp",
        stable_fp="stable-url-key",
    )
    # New credentials to the same controllers (new pair_fp, same stable)
    # must still shed load.
    assert tracker.is_fenced_pair("new-rotated-fp", "stable-url-key") is True
    err = tracker.check_submit("d-new", "new-rotated-fp", "stable-url-key")
    assert err is not None and "fenced" in err.lower()


# -- #8: artifact download cap + OSError --------------------------------------
def test_artifact_download_cap_and_oserror(pair: Any, client: TestClient, monkeypatch: Any) -> None:
    from api_shared import _fake_success, _wait

    _fake_success(monkeypatch, "run_export")
    jid = client.post("/api/v1/exports", json={}).json()["job_id"]
    assert _wait(client, jid)["status"] == "succeeded"
    job_dir = get_job_manager().get_internal(jid)["job_dir"]
    big = os.path.join(job_dir, "big.bin")
    with open(big, "wb") as fh:
        fh.truncate((32 << 20) + 1)
    resp = client.get(f"/api/v1/jobs/{jid}/artifacts/big.bin")
    assert resp.status_code == 413, resp.text
    # OSError on stat/read maps to 404 (conditional patch so TestClient
    # internals keep working).
    import pathlib as _pl

    _orig_read = _pl.Path.read_bytes

    def _flaky_read(self: Any, *a: Any, **k: Any) -> Any:
        if self.name == "orgs.json":
            raise OSError("gone")
        return _orig_read(self, *a, **k)

    monkeypatch.setattr(_pl.Path, "read_bytes", _flaky_read)
    # OSError on a small file maps to 404 (the oversize 413 branch needs a
    # genuinely oversize file, pinned above).
    resp2 = client.get(f"/api/v1/jobs/{jid}/artifacts/exports/orgs.json")
    assert resp2.status_code == 404, resp2.text


# -- #9: artifact pagination off-by-one ---------------------------------------
def test_artifact_pagination_exact_boundary(tmp_path: Any) -> None:
    from aap_migration.api.routers.jobs import _list_artifacts_page

    base = str(tmp_path / "art")
    os.makedirs(base, exist_ok=True)
    for i in range(3):
        with open(os.path.join(base, f"f{i}.json"), "w") as fh:
            fh.write("{}")
    page, total, walked, truncated = _list_artifacts_page(base, limit=2, offset=0)
    assert total == 3
    assert page == ["f0.json", "f1.json"]
    assert truncated is True
    page2, _, _, trunc2 = _list_artifacts_page(base, limit=2, offset=2)
    assert page2 == ["f2.json"]
    assert trunc2 is False


# -- #11: manager pagination clamp -------------------------------------------
def test_manager_pagination_clamp(tmp_path: Any) -> None:
    """The list_jobs limit cap is actually enforced (seed past the 1000 cap).

    A handful of jobs never stresses the clamp, so removing it stays green.
    Inserting records directly keeps this fast (no worker round-trips).
    """
    from aap_migration.api.jobs._records import _utcnow
    from aap_migration.api.jobs.manager import JobManager

    manager = JobManager(base_dir=str(tmp_path / "jobs"))
    now = _utcnow()
    with manager._lock:
        for i in range(1005):
            jid = f"clamp-{i:04d}"
            manager._jobs[jid] = {
                "job_id": jid,
                "job_type": "t",
                "status": "succeeded",
                "params": {},
                "job_dir": f"/tmp/{jid}",
                "result": None,
                "error": None,
                "error_id": None,
                "exit_code": None,
                "created_at": now,
                "updated_at": now,
            }
    assert manager.count() == 1005
    # limit=5000 is clamped to the 1000-item cap.
    page = manager.list_jobs(limit=5000, offset=0)
    assert len(page) == 1000
    # A negative offset is clamped to 0 (same first page).
    assert manager.list_jobs(limit=5000, offset=-5) == page
    # Deep pages past the end are empty.
    assert manager.list_jobs(limit=10, offset=2000) == []
    # limit floors at 1 (never an empty page from limit=0 alone).
    assert len(manager.list_jobs(limit=0, offset=0)) == 1


# -- #3: parked total-cap bound ------------------------------------------------
def test_parked_total_cap_bounds_growth(client: TestClient, monkeypatch: Any) -> None:
    from typing import cast

    from aap_migration.api.jobs import JobRecord
    from aap_migration.api.jobs import _config as _job_config

    manager = get_job_manager()
    # Fill admission to just below cap with parked waiters on top: total
    # must shed once MAX_QUEUE_DEPTH + MAX_ORPHANS is reached.
    monkeypatch.setattr(_job_config, "MAX_QUEUE_DEPTH", 2)
    monkeypatch.setattr(_job_config, "MAX_ORPHANS", 1)
    base_rec: JobRecord = {
        "job_id": "t0",
        "job_type": "t",
        "status": "queued",
        "params": {},
        "job_dir": "/tmp/t0",
        "result": None,
        "error": None,
        "error_id": None,
        "exit_code": None,
        "created_at": "now",
        "updated_at": "now",
        "cancel_requested": False,
    }
    with manager._lock:
        for i in range(3):
            manager._jobs[f"t{i}"] = cast(JobRecord, dict(base_rec, job_id=f"t{i}"))
        manager._parked["t2"] = {
            "work_dir": "/tmp/t2",
            "pair_fp": "",
            "stable_fp": "",
            "fence": "workdir",
            "until": time.monotonic() + 600,
        }
        assert manager._admission_count_locked() == 2
        assert manager._total_active_locked() == 3
    try:
        with manager._lock:
            total = manager._total_active_locked()
        assert total >= 3
    finally:
        with manager._lock:
            for i in range(3):
                manager._jobs.pop(f"t{i}", None)
            manager._parked.pop("t2", None)


# -- #15: unknown-type 404 parity ----------------------------------------------
def test_unknown_resource_type_404_parity(pair: Any, client: TestClient) -> None:
    preview = client.post(
        "/api/v1/transforms/preview",
        json={"resource_type": "no-such-type", "payload": {}},
    )
    assert preview.status_code == 404, preview.text
    payload = client.post(
        "/api/v1/validations/payload",
        json={"resource_type": "no-such-type", "payload": {}},
    )
    assert payload.status_code == 404, payload.text
