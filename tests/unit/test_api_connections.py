"""REST API tests: connection CRUD + auth guards."""

import os
from typing import Any

from api_shared import _fake_success, _wait
from fastapi.testclient import TestClient


class TestConnections:
    def test_crud_no_token_in_public(self, client: TestClient) -> None:
        created = client.post(
            "/api/v1/connections",
            json={
                "name": "a",
                "kind": "source",
                "url": "https://a.example.com/api/v2",
                "token": "tok",
            },
        )
        assert created.status_code == 201
        assert "token" not in created.json()
        conn_id = created.json()["id"]

        assert client.get(f"/api/v1/connections/{conn_id}").status_code == 200
        assert "token" not in client.get(f"/api/v1/connections/{conn_id}").json()
        updated = client.patch(f"/api/v1/connections/{conn_id}", json={"timeout": 45}).json()
        assert updated["timeout"] == 45
        assert "token" not in updated

        deleted = client.delete(f"/api/v1/connections/{conn_id}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted_connection_id"] == conn_id
        assert client.get(f"/api/v1/connections/{conn_id}").status_code == 404

    def test_validation(self, client: TestClient) -> None:
        # bad kind rejected by request schema (422)
        assert (
            client.post(
                "/api/v1/connections",
                json={
                    "name": "x",
                    "kind": "nope",
                    "url": "https://a.example.com",
                    "token": "t",
                },
            ).status_code
            == 422
        )
        # non-https rejected like the CLI
        assert (
            client.post(
                "/api/v1/connections",
                json={
                    "name": "y",
                    "kind": "source",
                    "url": "http://a.example.com",
                    "token": "t",
                },
            ).status_code
            == 400
        )
        # duplicate name
        payload = {
            "name": "dup",
            "kind": "source",
            "url": "https://a.example.com",
            "token": "t",
        }
        assert client.post("/api/v1/connections", json=payload).status_code == 201
        assert client.post("/api/v1/connections", json=payload).status_code == 400
        # embedded credentials rejected at the API boundary (400, not stored)
        assert (
            client.post(
                "/api/v1/connections",
                json={
                    "name": "userinfo",
                    "kind": "source",
                    "url": "https://user:pass@a.example.com/api/v2",
                    "token": "t",
                },
            ).status_code
            == 400
        )
        # non-http(s) schemes rejected at the API boundary (400)
        assert (
            client.post(
                "/api/v1/connections",
                json={
                    "name": "gopher",
                    "kind": "source",
                    "url": "gopher://a.example.com/api/v2",
                    "token": "t",
                },
            ).status_code
            == 400
        )

    def test_metadata_url_blocked(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/connections",
            json={
                "name": "meta",
                "kind": "source",
                "url": "https://169.254.169.254/latest/meta-data/",
                "token": "t",
            },
        )
        assert resp.status_code == 400

    def test_active_requires_existing(self, client: TestClient) -> None:
        assert (
            client.post("/api/v1/connections/active", json={"source_id": "missing"}).status_code
            == 404
        )

    def test_tokens_encrypted_at_rest(
        self, client: TestClient, tmp_path: Any, monkeypatch: Any
    ) -> None:
        created = client.post(
            "/api/v1/connections",
            json={
                "name": "enc",
                "kind": "source",
                "url": "https://a.example.com",
                "token": "super-secret-value",
            },
        ).json()
        assert "token" not in created
        listed = client.get("/api/v1/connections").json()
        assert set(listed) >= {"items", "total", "limit", "offset"}
        assert listed["total"] == len(listed["items"])
        assert all("token" not in c for c in listed["items"])
        # Open the SQLite row: ciphertext must differ and round-trip.
        import sqlite3

        db = os.environ["AAP_BRIDGE_API_DB"]
        con = sqlite3.connect(db)
        try:
            row = con.execute("SELECT token_encrypted FROM api_connections").fetchone()
        finally:
            con.close()
        assert row is not None
        assert row[0] != "super-secret-value"
        from aap_migration.api.security import decrypt_token

        assert decrypt_token(row[0]) == "super-secret-value"

    def test_auth_gating(self, client: TestClient, monkeypatch: Any) -> None:
        monkeypatch.setenv("AAP_BRIDGE_API_TOKEN", "test-key-123")
        # No key -> 401 (auth dependency active when env is set).
        resp = client.get("/api/v1/health")
        assert resp.status_code == 401
        # Wrong key -> 401.
        assert client.get("/api/v1/health", headers={"X-API-Key": "nope"}).status_code == 401
        # Right key -> 200.
        assert (
            client.get("/api/v1/health", headers={"X-API-Key": "test-key-123"}).status_code == 200
        )


class TestGuards:
    def test_jobs_need_connections(self, client: TestClient) -> None:
        # fresh DB: no active pair -> immediate 400, not 202
        assert client.post("/api/v1/exports", json={}).status_code == 400
        assert client.post("/api/v1/migrations", json={}).status_code == 400
        assert client.post("/api/v1/validations", json={}).status_code == 400

    def test_option_guards(self, pair: Any, client: TestClient) -> None:
        # Schema-level mutual exclusions surface as 422 (single home).
        assert (
            client.post(
                "/api/v1/iam/migrate",
                json={"skip_user_roles": True, "users_only": True},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/validations",
                json={"skip_hosts": True, "resource_type": "hosts"},
            ).status_code
            == 422
        )
        assert client.post("/api/v1/analysis/dependencies", json={}).status_code == 422
        assert (
            client.post("/api/v1/migrations/resume", json={"from_phase": "nope"}).status_code == 422
        )
        assert client.post("/api/v1/iam/report", json={"job_id": "missing"}).status_code == 404
        assert client.post("/api/v1/exports", json={"job_id": "missing"}).status_code == 404

    def test_chaining_rejects_nonsucceeded(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        import threading

        import aap_migration.api.services as services_mod

        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "export ok"}

        monkeypatch.setattr(services_mod, "run_export", _blocker)
        first = client.post("/api/v1/exports", json={}).json()
        assert entered.wait(timeout=30)
        try:
            # Chaining onto a running job -> deterministic 409 (not silent partial).
            resp = client.post("/api/v1/transforms", json={"job_id": first["job_id"]})
            assert resp.status_code == 409
            assert "only succeeded jobs" in resp.json()["detail"]
        finally:
            release.set()
        done = _wait(client, first["job_id"])
        assert done["status"] == "succeeded"
        # Chaining onto the succeeded job is accepted.
        _fake_success(monkeypatch, "run_transform", artifact="xformed/orgs.json")
        second = client.post("/api/v1/transforms", json={"job_id": first["job_id"]})
        assert second.status_code == 202

    def test_pair_switch_requires_opt_in(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        _fake_success(monkeypatch, "run_export")
        first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert first["status"] == "succeeded"
        other_src = client.post(
            "/api/v1/connections",
            json={
                "name": "other-src",
                "kind": "source",
                "url": "https://other.example.com/api/v2",
                "token": "x",
            },
        ).json()
        resp = client.post(
            "/api/v1/transforms",
            json={"job_id": first["job_id"], "source_id": other_src["id"]},
        )
        assert resp.status_code == 400
        assert "allow_pair_switch" in resp.json()["detail"]

    def test_delete_referenced_connection_conflict(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """Deleting a connection referenced by a queued/running job returns 409."""
        import threading

        from aap_migration.api.jobs import get_job_manager

        manager = get_job_manager()
        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "blocker"}

        src, _tgt = pair
        manager.submit("blocker", {"source_id": src["id"]}, _blocker)
        assert entered.wait(timeout=30)
        try:
            resp = client.delete(f"/api/v1/connections/{src['id']}")
            assert resp.status_code == 409, resp.text
            assert "queued/running job" in resp.json()["detail"]
        finally:
            release.set()
        # Drain the blocker before deleting.
        deadline = __import__("time").time() + 30
        while manager.has_active_jobs() and __import__("time").time() < deadline:
            __import__("time").sleep(0.05)
        assert client.delete(f"/api/v1/connections/{src['id']}").status_code == 200

    def test_connection_test_success_and_redaction(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """POST /connections/{id}/test 200 shape and 502 redaction."""
        import aap_migration.client.aap_source_client as src_mod
        import aap_migration.utils.ssrf as ssrf_mod

        src, _tgt = pair
        # Example.com URLs do not resolve in CI: stub the execution-time
        # re-verification (create-time validation already ran at POST).
        monkeypatch.setattr(ssrf_mod, "reverify_execution_url_bounded", lambda url: None)

        class _FakeSource:
            def __init__(self, **kwargs: Any) -> None:
                pass

            async def get(self, *args: Any, **kwargs: Any) -> Any:
                return {}

            async def get_version(self) -> str:
                return "2.6.1"

            async def aclose(self) -> None:
                pass

        monkeypatch.setattr(src_mod, "AAPSourceClient", _FakeSource)
        resp = client.post(f"/api/v1/connections/{src['id']}/test")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["reachable"] is True
        assert body["connection_id"] == src["id"]
        assert "token" not in body
        assert "s3cret" not in resp.text

        class _BoomSource(_FakeSource):
            async def get(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("backend-boom-detail-should-not-leak")

        monkeypatch.setattr(src_mod, "AAPSourceClient", _BoomSource)
        failed = client.post(f"/api/v1/connections/{src['id']}/test")
        assert failed.status_code == 502, failed.text
        assert "backend-boom-detail" not in failed.text

    def test_patch_alias_and_active_clear(self, client: TestClient) -> None:
        """PATCH is partial-update; PUT is full-replace (all fields required)."""
        created = client.post(
            "/api/v1/connections",
            json={
                "name": "patch-me",
                "kind": "source",
                "url": "https://p.example.com/api/v2",
                "token": "t",
            },
        ).json()
        conn_id = created["id"]
        patched = client.patch(f"/api/v1/connections/{conn_id}", json={"timeout": 60})
        assert patched.status_code == 200, patched.text
        assert patched.json()["timeout"] == 60
        via_put = client.put(
            f"/api/v1/connections/{conn_id}",
            json={
                "name": "patch-me",
                "url": "https://p.example.com/api/v2",
                "token": "t",
                "verify_ssl": True,
                "timeout": 61,
            },
        )
        assert via_put.status_code == 200, via_put.text
        assert via_put.json()["timeout"] == 61
        # PUT requires the full body: partial payloads 422.
        partial_put = client.put(f"/api/v1/connections/{conn_id}", json={"timeout": 62})
        assert partial_put.status_code == 422, partial_put.text

        tgt = client.post(
            "/api/v1/connections",
            json={
                "name": "patch-tgt",
                "kind": "target",
                "url": "https://q.example.com/api/v2",
                "token": "t",
            },
        ).json()
        active = client.post(
            "/api/v1/connections/active",
            json={"source_id": conn_id, "target_id": tgt["id"]},
        )
        assert active.status_code == 200, active.text
        # Explicit null is a no-op (keeps the current value)...
        kept = client.post("/api/v1/connections/active", json={"source_id": None}).json()
        assert kept["source_id"] == conn_id
        # ...while clear flags detach.
        cleared = client.post("/api/v1/connections/active", json={"clear_source": True}).json()
        assert cleared["source_id"] is None
        assert cleared["target_id"] == tgt["id"]
        wiped = client.delete("/api/v1/connections/active?target=true").json()
        assert wiped["target_id"] is None
