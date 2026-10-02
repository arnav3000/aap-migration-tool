"""Shared pytest fixtures for tests/unit (auto-loaded, no import needed)."""

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AAP_BRIDGE_API_DB", "/tmp/test_api_state.db")
os.environ.setdefault("AAP_BRIDGE_JOB_DIR", "/tmp/test_api_jobs")

from aap_migration.api.app import create_app  # noqa: E402
from aap_migration.api.jobs import reset_job_manager  # noqa: E402


@pytest.fixture()
def client(tmp_path: Any, monkeypatch: Any) -> Iterator[TestClient]:
    db = str(tmp_path / "api.db")
    jobs = str(tmp_path / "jobs")
    monkeypatch.setenv("AAP_BRIDGE_API_DB", db)
    monkeypatch.setenv("AAP_BRIDGE_JOB_DIR", jobs)
    # Isolate server-default state lookups per test: without this, tests
    # that resolve the default DB create ./database/migration_state.db in
    # the repo checkout (and leak state across tests).
    monkeypatch.setenv("AAP_BRIDGE_STARTUP_CWD", str(tmp_path))
    monkeypatch.delenv("AAP_BRIDGE_API_TOKEN", raising=False)
    # Fail-closed auth (security.require_api_key) rejects tokenless requests
    # unless the explicit dev opt-in is set: the suite exercises routes
    # without a token, so opt in here. Dedicated auth tests override this.
    monkeypatch.setenv("AAP_BRIDGE_ALLOW_ANON", "1")
    reset_job_manager(base_dir=jobs)
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture()
def pair(client: TestClient) -> tuple:
    src = client.post(
        "/api/v1/connections",
        json={
            "name": "src",
            "kind": "source",
            "url": "https://src.example.com/api/v2",
            "token": "s3cret",
            "verify_ssl": False,
        },
    ).json()
    tgt = client.post(
        "/api/v1/connections",
        json={
            "name": "tgt",
            "kind": "target",
            "url": "https://tgt.example.com/api/controller/v2",
            "token": "t0p",
            "verify_ssl": False,
        },
    ).json()
    client.post(
        "/api/v1/connections/active",
        json={"source_id": src["id"], "target_id": tgt["id"]},
    )
    return src, tgt
