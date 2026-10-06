"""Submit-path guards: pinning, fail-closed gates, and fail-fast submit (#1, #3, #9).

Covers ``routers._common.submit_chained`` / ``submit_job`` directly with a
stubbed-or-real job manager: every connection-bearing submit must carry
complete ``_snapshot_*`` pins (P0 #1), the TLS/pair-switch/chaining gates
must fail closed (P1 #3), and direct ``submit_job`` calls must fail fast
instead of after the queue wait (P2 #9).
"""

from typing import Any

import pytest
from api_shared import _wait
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch: Any) -> Any:
    """Creation-time SSRF checks must not need real DNS in this sandbox.

    Mirrors the existing store tests (``AAP_BRIDGE_SSRF_ALLOW_PRIVATE=1``)
    and neutralizes execution-time re-verification so background workers
    can run against ``*.example.com`` fixtures.
    """
    monkeypatch.setenv("AAP_BRIDGE_SSRF_ALLOW_PRIVATE", "1")
    import aap_migration.utils.ssrf as ssrf_mod

    monkeypatch.setattr(ssrf_mod, "reverify_execution_url_bounded", lambda url: url)
    monkeypatch.setattr(ssrf_mod, "reverify_execution_url", lambda url: url)
    yield


def _ok(job: Any) -> Any:
    return {"message": "ok"}


class TestSubmitPins:
    """P0 #1: every connection-bearing submit carries snapshot pins."""

    def test_chained_submit_pins_pair(self, client: Any, pair: Any) -> None:
        from aap_migration.api.jobs import get_job_manager
        from aap_migration.api.routers._common import submit_chained
        from aap_migration.api.schemas import ConnectionSelector

        created = submit_chained("probe", ConnectionSelector(), _ok, need="both")
        assert created.job_id
        params = get_job_manager().get_internal(created.job_id)["params"]
        for key in (
            "_snapshot_source_id",
            "_snapshot_target_id",
            "_snapshot_fp",
            "_snapshot_need",
        ):
            assert key in params, key
        src, _tgt = pair
        assert params["_snapshot_source_id"] == src["id"]

    def test_source_only_submit_pins_source(self, client: Any, pair: Any) -> None:
        from aap_migration.api.jobs import get_job_manager
        from aap_migration.api.routers._common import submit_chained
        from aap_migration.api.schemas.etl import IamBenchmarkRequest

        created = submit_chained(
            "bench", IamBenchmarkRequest(sample_size=5, workers=[1]), _ok, need="source"
        )
        params = get_job_manager().get_internal(created.job_id)["params"]
        assert params["_snapshot_need"] == "source"
        assert params["_snapshot_fp"]


class TestChainedGates:
    """P1 #3: chaining, TLS-posture, and drift gates fail closed."""

    def test_unknown_job_id_is_404(self, client: Any, pair: Any) -> None:
        from aap_migration.api.routers._common import submit_chained
        from aap_migration.api.schemas import ChainedRequest

        with pytest.raises(HTTPException) as exc_info:
            submit_chained("probe", ChainedRequest(job_id="does-not-exist"), _ok, need="both")
        assert exc_info.value.status_code == 404

    def test_pair_switch_without_opt_in_is_400(self, client: Any, pair: Any) -> None:
        from aap_migration.api.routers._common import submit_chained
        from aap_migration.api.schemas import ChainedRequest

        first = submit_chained("probe", ChainedRequest(), _ok, need="both")
        _wait(client, first.job_id)
        other = client.post(
            "/api/v1/connections",
            json={
                "name": "other-src",
                "kind": "source",
                "url": "https://other.example.com/api/v2",
                "token": "s3cret",
                "verify_ssl": False,
            },
        ).json()
        with pytest.raises(HTTPException) as exc_info:
            submit_chained(
                "probe",
                ChainedRequest(job_id=first.job_id, source_id=other["id"]),
                _ok,
                need="both",
            )
        assert exc_info.value.status_code == 400
        assert "allow_pair_switch" in str(exc_info.value.detail)

    def test_tls_weakening_is_400(self, client: Any, pair: Any) -> None:
        from aap_migration.api.routers._common import submit_chained
        from aap_migration.api.schemas.etl import IamBenchmarkRequest

        strict = client.post(
            "/api/v1/connections",
            json={
                "name": "strict-src",
                "kind": "source",
                "url": "https://strict.example.com/api/v2",
                "token": "s3cret",
                "verify_ssl": True,
            },
        ).json()
        client.post("/api/v1/connections/active", json={"source_id": strict["id"]})
        with pytest.raises(HTTPException) as exc_info:
            submit_chained(
                "bench",
                IamBenchmarkRequest(verify_ssl=False, sample_size=5, workers=[1]),
                _ok,
                need="source",
            )
        assert exc_info.value.status_code == 400
        assert "verify_ssl" in str(exc_info.value.detail)

    def test_drift_between_fingerprint_and_enqueue_is_409(
        self, client: Any, pair: Any, monkeypatch: Any
    ) -> None:
        import threading
        import time

        import aap_migration.api.store as store_mod
        from aap_migration.api.jobs import get_job_manager
        from aap_migration.api.routers._common import submit_chained, submit_job
        from aap_migration.api.schemas import ConnectionSelector

        # Occupy the single FIFO lane so the drifted submit stays queued:
        # fail_fast only fails queued jobs (an instantly-dequeued job
        # exercises the execution-time drift check instead).
        gate = threading.Event()

        def _block(job: Any) -> Any:
            assert gate.wait(30)
            return {"message": "released"}

        lane = submit_job("lane-blocker", {}, _block)
        deadline = time.time() + 10
        while get_job_manager().get(lane.job_id)["status"] != "running":
            assert time.time() < deadline, "lane blocker never started"
            time.sleep(0.05)
        try:
            real = store_mod.pair_fingerprint
            calls = {"n": 0}

            def _drift_once(scope: Any) -> Any:
                out = real(scope)
                calls["n"] += 1
                if calls["n"] == 2:
                    # A rotation landing after the submit-time snapshot: the
                    # re-verification fingerprint no longer matches the pins.
                    out = dict(out)
                    out["fp"] = f"{out['fp']}-rotated"
                return out

            monkeypatch.setattr(store_mod, "pair_fingerprint", _drift_once)
            with pytest.raises(HTTPException) as exc_info:
                submit_chained("probe", ConnectionSelector(), _ok, need="both")
            assert exc_info.value.status_code == 409
            assert "resubmit" in str(exc_info.value.detail).lower()
        finally:
            gate.set()


class TestDirectSubmitGuards:
    """P2 #9: direct submit_job fails fast on unpinned connection params."""

    def test_selectors_without_pins_are_400(self, client: Any) -> None:
        from aap_migration.api.routers._common import submit_job

        with pytest.raises(ValueError, match="submit_chained"):
            submit_job("probe", {"source_id": "src-1"}, _ok)

    def test_incomplete_pins_are_400(self, client: Any) -> None:
        from aap_migration.api.routers._common import submit_job

        with pytest.raises(ValueError, match="pin key"):
            submit_job(
                "probe",
                {"_snapshot_need": "both", "_snapshot_source_id": "src-1"},
                _ok,
            )

    def test_connectionless_submit_passes(self, client: Any) -> None:
        from aap_migration.api.jobs import get_job_manager
        from aap_migration.api.routers._common import submit_job

        created = submit_job("probe", {"note": "connectionless"}, _ok)
        assert created.job_id
        assert get_job_manager().get(created.job_id)["job_id"] == created.job_id

    def test_chained_report_style_job_id_passes(self, client: Any) -> None:
        from aap_migration.api.routers._common import submit_job

        # need="none" chained submits (e.g. iam-report re-rendering onto a
        # referenced workdir) carry job_id without pins: legitimate.
        created = submit_job("report", {"job_id": "some-prior", "json_path": "x"}, _ok)
        assert created.job_id
