"""P2 #11: total is true pre-page count; walked is bounded-walk count."""

from pathlib import Path
from typing import Any

import pytest
from api_shared import _fake_success, _wait
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "limit,offset,exp_len,exp_walked",
    [
        (2, 0, 2, 3),  # limit+offset+1 bounded walk
        (2, 1, 2, 4),
        (1, 0, 1, 2),
    ],
)
def test_artifacts_total_truth_walked_bounded(
    pair: Any,
    client: TestClient,
    monkeypatch: Any,
    limit: int,
    offset: int,
    exp_len: int,
    exp_walked: int,
) -> None:
    """Table-driven page slices: total stays true, walked stays bounded.

    Owning contract (truncation/int-total/secret exclusions) lives in
    TestJobs.test_artifacts_truncation_keeps_int_total; this table covers
    distinct limit/offset slices instead of duplicating it.
    """
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
    assert true_total == len(full["artifacts"])
    assert full["walked"] == true_total

    paged = client.get(
        f"/api/v1/jobs/{job['job_id']}/artifacts?limit={limit}&offset={offset}"
    ).json()
    assert paged["truncated"] is True
    assert len(paged["artifacts"]) == exp_len
    assert paged["total"] == true_total
    assert paged["walked"] == exp_walked
    assert paged["total"] != paged["walked"]
