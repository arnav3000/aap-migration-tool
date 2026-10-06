"""Shared helpers for the AAP Bridge REST API test modules.

Split from the former single ``test_api.py`` module alongside
``test_api_jobs.py``, ``test_api_connections.py``,
``test_api_system.py``, and ``test_api_validators.py``. Fixtures
(``client``, ``pair``) live in ``conftest.py``; this module holds only
plain helpers.
"""

import time
from typing import Any

from fastapi.testclient import TestClient


def _wait(client: TestClient, job_id: str, timeout: int = 30) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    job = client.get(f"/api/v1/jobs/{job_id}").json()
    raise TimeoutError(
        f"job {job_id} did not finish in {timeout}s "
        f"(status={job.get('status')}) console={(job.get('error') or '')[:500]}"
    )


def _fake_success(monkeypatch: Any, name: str, artifact: str = "exports/orgs.json") -> None:
    """Patch a services.run_* worker to succeed fast with a dummy artifact.

    Mirrors the explicit-[] no-op contract (omitted/None means all,
    explicit [] selects none): fakes return the no-op result for
    explicit-[] params like the real workers do.
    """
    import aap_migration.api.services as services_mod
    from aap_migration.api.services._core import is_noop_scope, noop_result

    def _fake(job: Any) -> Any:
        from pathlib import Path

        # Delegate the [] branch to the real guard AND the canonical
        # envelope so the fake cannot diverge from the worker on either.
        if is_noop_scope(job.get("params", {})):
            return noop_result()
        workdir = Path(job["job_dir"]).resolve()
        target = workdir / artifact
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
        return {"message": f"{name} ok", "artifacts": [artifact]}

    monkeypatch.setattr(services_mod, name, _fake)
