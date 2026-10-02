"""Fixgroup A: internal snapshot keys stay server-side (P3 #26).

Underscore-prefixed internal keys (``_snapshot_*``) must be stripped from
the public job-params view served by job polling, while the internal
record keeps them for execution-time verification.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from aap_migration.api.routers._common import public_params


def test_public_params_strips_underscore_keys() -> None:
    params = {
        "source_id": "a",
        "resource_types": ["organizations"],
        "allow_pair_switch": False,
        "_snapshot_source_id": "a",
        "_snapshot_target_id": "b",
        "_snapshot_fp": "fp",
        "_snapshot_need": "both",
        "_snapshot_fernet_fp": "fernet",
    }
    pub = public_params(params)
    assert pub == {
        "source_id": "a",
        "resource_types": ["organizations"],
        "allow_pair_switch": False,
    }
    # Server-side record keeps the pins.
    assert params["_snapshot_fp"] == "fp"


def test_polling_view_hides_snapshot_keys(pair: Any, client: TestClient, monkeypatch: Any) -> None:
    from api_shared import _fake_success, _wait

    _fake_success(monkeypatch, "run_export")
    jid = client.post("/api/v1/exports", json={}).json()["job_id"]
    job = _wait(client, jid)
    assert job["status"] == "succeeded"
    assert all(not key.startswith("_") for key in job.get("params", {})), job["params"]
    listed = client.get("/api/v1/jobs").json()["items"][0]
    assert all(not key.startswith("_") for key in listed.get("params", {}))
    # Internal record still pins server-side.
    from aap_migration.api.jobs import get_job_manager

    internal = get_job_manager().get_internal(jid)
    assert internal["params"].get("_snapshot_fp")
