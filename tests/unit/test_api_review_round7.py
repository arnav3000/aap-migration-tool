"""Review round 7: pin the round-6 findings with failing-first tests.

#1: post-submit pair-drift guard needs a drift-path test (409 + failed
    job) and a fingerprint-exception case (fail_fast + mapped error).
#2: checkpoint ``migration_id`` all-sentinel reuse needs a semantics test
    (omitted vs uuid vs literal-all listing truthfulness).
#3: AnalyzeDependenciesRequest must not document explicit [] as a no-op
    (schema 422s it); explicit [] stays distinct from omission.
#4: IAM ``_source_or_fallback`` must fail loudly on malformed configs
    (ValueError) while keeping the stub-only AttributeError fallback.
#6: connection-probe TimeoutError and HTTPException branches need
    error-path tests (502 timed-out detail, passthrough).
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient


def _localhost_pair(client: TestClient, monkeypatch: Any) -> tuple:
    """Create + activate a pair without DNS (127.0.0.1 needs no resolver).

    The sandbox under review may block getaddrinfo entirely, and create-time
    SSRF validation resolves every host: opt into the documented private
    escape hatch so 127.0.0.1 skips resolution (metadata endpoints stay
    blocked regardless).
    """
    monkeypatch.setenv("AAP_BRIDGE_SSRF_ALLOW_PRIVATE", "1")
    src = client.post(
        "/api/v1/connections",
        json={
            "name": "lsrc",
            "kind": "source",
            "url": "https://127.0.0.1:9/api/v2",
            "token": "t",
            "verify_ssl": False,
        },
    ).json()
    tgt = client.post(
        "/api/v1/connections",
        json={
            "name": "ltgt",
            "kind": "target",
            "url": "https://127.0.0.1:9/api/v2",
            "token": "t",
            "verify_ssl": False,
        },
    ).json()
    client.post(
        "/api/v1/connections/active",
        json={"source_id": src["id"], "target_id": tgt["id"]},
    )
    return src, tgt


def _occupy_worker(client: TestClient, monkeypatch: Any) -> Any:
    """Park one blocking job on the single FIFO worker.

    The worker wakes the instant a job is enqueued, so a drift test that
    asserts 409 must hold the worker busy: otherwise the worker can grab
    the drifted job before the post-submit recheck fails it (fail_fast
    returns False past queued state and the submit answers 202).
    """
    import threading
    import time

    import aap_migration.api.services as services_mod

    gate = threading.Event()

    def _blocking_export(job: Any) -> Any:
        assert gate.wait(timeout=60), "worker blocker released too late"
        return {"message": "blocker ok", "artifacts": []}

    monkeypatch.setattr(services_mod, "run_export", _blocking_export)
    first = client.post("/api/v1/exports", json={})
    assert first.status_code == 202, first.text
    job_id = first.json()["job_id"]
    # Wait until the worker actually picked the job up (running), not
    # terminal: the blocker only finishes after the test releases it.
    deadline = time.time() + 30
    while time.time() < deadline:
        seen = client.get(f"/api/v1/jobs/{job_id}").json()
        if seen["status"] == "running":
            return gate
        if seen["status"] in ("succeeded", "failed", "cancelled"):
            pytest.fail(f"blocker job ended early: {seen}")
        time.sleep(0.05)
    pytest.fail(f"worker never picked up blocker job: {seen}")


class TestPostSubmitDrift:
    """#1: the post-enqueue recheck fails drifted submits fast with 409."""

    def test_drift_between_snapshot_and_enqueue_409(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        import aap_migration.api.store as store_mod

        _localhost_pair(client, monkeypatch)
        gate = _occupy_worker(client, monkeypatch)
        try:
            real_fp = store_mod.pair_fingerprint
            calls = {"n": 0}

            def _drifting_fp(*args: Any, **kwargs: Any) -> Any:
                calls["n"] += 1
                snap = real_fp(*args, **kwargs)
                if calls["n"] >= 2:
                    return {**snap, "fp": f"{snap['fp']}-drifted"}
                return snap

            monkeypatch.setattr(store_mod, "pair_fingerprint", _drifting_fp)
            resp = client.post("/api/v1/exports", json={})
            assert resp.status_code == 409, resp.text
            assert "resubmit" in resp.json()["detail"].lower()
            jobs = client.get("/api/v1/jobs").json()
            failed = [j for j in jobs["items"] if j["status"] == "failed"]
            assert failed, "drifted job must be failed immediately, not queued"
            assert "resubmit" in (failed[0].get("error") or "").lower()
        finally:
            gate.set()

    def test_fingerprint_exception_fails_fast_with_mapped_error(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        import aap_migration.api.store as store_mod

        _localhost_pair(client, monkeypatch)
        gate = _occupy_worker(client, monkeypatch)
        try:
            real_fp = store_mod.pair_fingerprint
            calls = {"n": 0}

            def _flapping_fp(*args: Any, **kwargs: Any) -> Any:
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise KeyError("no active pair")
                return real_fp(*args, **kwargs)

            monkeypatch.setattr(store_mod, "pair_fingerprint", _flapping_fp)
            resp = client.post("/api/v1/exports", json={})
            # KeyError maps to 404 via the single-home store error mapping.
            assert resp.status_code == 404, resp.text
            jobs = client.get("/api/v1/jobs").json()
            failed = [j for j in jobs["items"] if j["status"] == "failed"]
            assert failed, "job must be failed immediately on recheck error"
            assert "resubmit" in (failed[0].get("error") or "").lower()
        finally:
            gate.set()


class TestCheckpointSentinelSemantics:
    """#2: omitted vs uuid vs literal-all checkpoint listings are truthful."""

    def test_migration_id_filter_semantics(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from aap_migration.config import StateConfig
        from aap_migration.migration.checkpoint import CheckpointManager
        from aap_migration.migration.state import MigrationState

        db = str(tmp_path / "sentinel.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        id_a = "11111111-1111-4111-8111-111111111111"
        id_b = "22222222-2222-4222-8222-222222222222"
        with MigrationState(config=StateConfig(db_path=db), migration_id=id_a) as state_a:
            cpa = CheckpointManager(state_a).create_checkpoint(phase="export")
        with MigrationState(config=StateConfig(db_path=db), migration_id=id_b) as state_b:
            cpb = CheckpointManager(state_b).create_checkpoint(phase="export")
        assert cpa != cpb

        listed = client.get("/api/v1/checkpoints").json()
        assert listed["total"] == 2, listed
        assert {c["id"] for c in listed["checkpoints"]} == {cpa, cpb}

        only_a = client.get(f"/api/v1/checkpoints?migration_id={id_a}").json()
        assert only_a["total"] == 1, only_a
        assert {c["id"] for c in only_a["checkpoints"]} == {cpa}

        only_b = client.get(f"/api/v1/checkpoints?migration_id={id_b}").json()
        assert only_b["total"] == 1, only_b
        assert {c["id"] for c in only_b["checkpoints"]} == {cpb}

        # Literal 'all' collides with the manager list-all sentinel by
        # design: it must list all, never filter to empty.
        literal = client.get("/api/v1/checkpoints?migration_id=all").json()
        assert literal["total"] == 2, literal
        assert {c["id"] for c in literal["checkpoints"]} == {cpa, cpb}


class TestAnalyzeEmptyScopeContract:
    """#3: explicit [] without analyze_all is 422, never a silent no-op."""

    def test_explicit_empty_without_flag_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/analysis/dependencies", json={"organizations": []})
        assert resp.status_code == 422, resp.text
        assert "Must specify analyze_all=true" in resp.text

    def test_field_docs_do_not_promise_noop(self) -> None:
        from aap_migration.api.schemas import AnalyzeDependenciesRequest

        desc = AnalyzeDependenciesRequest.model_fields["organizations"].description
        assert desc is not None
        assert "is a no-op selecting none" not in desc
        assert "422" in desc


class TestIamFallbackGate:
    """#4: stub fallback stays stub-only; malformed configs fail loudly."""

    def test_real_config_never_double_resolves(self, monkeypatch: Any) -> None:
        import types

        import aap_migration.api.services.iam as iam
        from aap_migration.config import AAPInstanceConfig, MigrationConfig

        # True production shape: pydantic guarantees url/token are
        # non-empty strings, so the ctx path must win with no store read.
        config = MigrationConfig(
            source=AAPInstanceConfig(url="https://src.example.com/api/v2", token="s"),
            target=AAPInstanceConfig(url="https://tgt.example.com/api/v2", token="t"),
        )
        fake_ctx = types.SimpleNamespace(config=config)
        calls: list = []

        def _counting_connections(params: Any, need: Any = "source") -> Any:
            calls.append((dict(params), need))
            return ({"url": "u", "token": "t"}, None)

        monkeypatch.setattr(iam, "_iam_connections", _counting_connections)
        source, target = iam._source_or_fallback(fake_ctx, {}, need="both")
        assert calls == []
        assert source["url"] == "https://src.example.com/api/v2"
        assert target is not None and target["url"] == "https://tgt.example.com/api/v2"

    def test_stub_ctx_still_falls_back(self, monkeypatch: Any) -> None:
        import types

        import aap_migration.api.services.iam as iam

        # Wiring-spy shape: no .config at all (AttributeError -> fallback).
        stub_ctx = types.SimpleNamespace()
        sentinel = ({"url": "u", "token": "t"}, None)
        monkeypatch.setattr(iam, "_iam_connections", lambda params, need="source": sentinel)
        assert iam._source_or_fallback(stub_ctx, {}, need="source") == sentinel

    def test_malformed_config_fails_loudly(self, monkeypatch: Any) -> None:
        import types

        import aap_migration.api.services.iam as iam

        # Config present but url/token unusable: must raise ValueError
        # (fail loudly), never fall back to a second store resolution.
        bad_ctx = types.SimpleNamespace(
            config=types.SimpleNamespace(source=types.SimpleNamespace(url=None, token=None))
        )

        def _must_not_resolve(params: Any, need: Any = "source") -> Any:
            raise AssertionError("malformed config must not re-resolve via store")

        monkeypatch.setattr(iam, "_iam_connections", _must_not_resolve)
        with pytest.raises(ValueError, match="no url/token"):
            iam._source_or_fallback(bad_ctx, {}, need="source")


class TestProbeErrorPaths:
    """#6: probe TimeoutError -> 502 timed-out; HTTPException passes through."""

    def _fake_client_module(self, monkeypatch: Any, behavior: str) -> None:
        import aap_migration.client.aap_source_client as src_mod
        import aap_migration.utils.ssrf as ssrf_mod

        # Example.com URLs do not resolve in CI: stub execution-time
        # re-verification (create-time validation ran at POST).
        monkeypatch.setattr(ssrf_mod, "reverify_execution_url_bounded", lambda url: None)

        class _FakeSource:
            def __init__(self, **kwargs: Any) -> None:
                pass

            async def get(self, *args: Any, **kwargs: Any) -> Any:
                if behavior == "timeout":
                    raise TimeoutError("probe slow")
                if behavior == "passthrough":
                    from fastapi import HTTPException

                    raise HTTPException(status_code=418, detail="teapot-passthrough")
                raise AssertionError(f"unexpected behavior {behavior}")

            async def get_version(self) -> str:
                return "2.6.1"

            async def aclose(self) -> None:
                pass

        monkeypatch.setattr(src_mod, "AAPSourceClient", _FakeSource)

    def test_probe_timeout_502(self, client: TestClient, monkeypatch: Any) -> None:
        src, _tgt = _localhost_pair(client, monkeypatch)
        self._fake_client_module(monkeypatch, "timeout")
        resp = client.post(f"/api/v1/connections/{src['id']}/test")
        assert resp.status_code == 502, resp.text
        assert "timed out" in resp.json()["detail"].lower()

    def test_probe_http_exception_passthrough(self, client: TestClient, monkeypatch: Any) -> None:
        src, _tgt = _localhost_pair(client, monkeypatch)
        self._fake_client_module(monkeypatch, "passthrough")
        resp = client.post(f"/api/v1/connections/{src['id']}/test")
        assert resp.status_code == 418, resp.text
        assert "teapot-passthrough" in resp.text
