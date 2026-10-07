"""P2 #17 + #14 (G1): job-scoped depcheck must not judge the default dir.

When job_id is set but the chained xformed tree/state is absent, both
routes return 404 (transient: no transformed data yet) instead of 200 for
wrong data.
"""

from pathlib import Path
from typing import Any

from api_shared import _fake_success, _wait
from fastapi.testclient import TestClient


def test_validation_job_without_xformed_404(
    pair: Any, client: TestClient, monkeypatch: Any
) -> None:
    _fake_success(monkeypatch, "run_export")
    first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
    # Fake export creates exports/ but no xformed/ and no DB.
    resp = client.post("/api/v1/validations/dependencies", json={"job_id": first["job_id"]})
    assert resp.status_code == 404, resp.text
    assert "No transformed data yet" in resp.json()["detail"]


def test_validation_job_with_data_200(pair: Any, client: TestClient, monkeypatch: Any) -> None:
    from aap_migration.api.context import open_state
    from aap_migration.api.jobs import get_job_manager

    _fake_success(monkeypatch, "run_export")
    first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
    ref = get_job_manager().get_internal(first["job_id"])
    open_state(str(Path(ref["job_dir"]) / "migration_state.db"))
    (Path(ref["job_dir"]) / "xformed").mkdir(parents=True, exist_ok=True)
    resp = client.post("/api/v1/validations/dependencies", json={"job_id": first["job_id"]})
    assert resp.status_code == 200, resp.text
    assert "validation" in resp.json()


def test_import_job_without_state_404(pair: Any, client: TestClient, monkeypatch: Any) -> None:
    _fake_success(monkeypatch, "run_export")
    first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
    # No chained DB materialized: transient 404, never default-DB 200.
    resp = client.post("/api/v1/imports/check-dependencies", json={"job_id": first["job_id"]})
    assert resp.status_code == 404, resp.text
    assert "No transformed data yet" in resp.json()["detail"]


def test_import_job_with_db_200(pair: Any, client: TestClient, monkeypatch: Any) -> None:
    from pathlib import Path as _P

    from aap_migration.api.context import open_state
    from aap_migration.api.jobs import get_job_manager

    _fake_success(monkeypatch, "run_export")
    first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
    ref = get_job_manager().get_internal(first["job_id"])
    open_state(str(_P(ref["job_dir"]) / "migration_state.db"))
    # xformed absent but DB present: import closure is state-based, so 200
    # judges the chained DB (validation route owns the strict xformed gate).
    resp = client.post("/api/v1/imports/check-dependencies", json={"job_id": first["job_id"]})
    assert resp.status_code == 200, resp.text
    assert "missing" in resp.json()
