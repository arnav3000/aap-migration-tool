"""P2 #11: total is true pre-page count; walked is bounded-walk count."""

from pathlib import Path
from typing import Any

from api_shared import _fake_success, _wait
from fastapi.testclient import TestClient


def test_artifacts_total_truth_walked_bounded(
    pair: Any, client: TestClient, monkeypatch: Any
) -> None:
    from aap_migration.api.jobs import get_job_manager

    _fake_success(monkeypatch, "run_export")
    job = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
    ref = get_job_manager().get_internal(job["job_id"])
    workdir = Path(ref["job_dir"])
    for i in range(5):
        (workdir / f"extra-{i}.json").write_text("{}")

    full = client.get(f"/api/v1/jobs/{job['job_id']}/artifacts?limit=500").json()
    assert full["truncated"] is False
    true_total = full["total"]
    # Full walk: total equals page length when not truncated.
    assert true_total == len(full["artifacts"])
    assert full["walked"] == true_total

    paged = client.get(f"/api/v1/jobs/{job['job_id']}/artifacts?limit=2").json()
    assert paged["truncated"] is True
    assert len(paged["artifacts"]) == 2
    # P2 #11 contract: total stays true (not bounded 3), walked is bounded.
    assert paged["total"] == true_total
    assert paged["walked"] == 3  # limit+offset+1 bounded walk
    assert paged["total"] != paged["walked"]
