"""Connectivity error-mapping tests (#10).

The connections probe and config validation share one contract: SSRF
rejections are 400, timeouts are 502 with a timeout message, backend
failures are 502 with the redacted generic message (never backend
detail), and framework HTTP errors propagate unchanged.
"""

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch: Any) -> Any:
    monkeypatch.setenv("AAP_BRIDGE_SSRF_ALLOW_PRIVATE", "1")
    yield


def _stub_clients(monkeypatch: Any, behavior: str) -> None:
    """Patch client probes and SSRF re-verification for deterministic errors."""
    import aap_migration.client.aap_source_client as source_mod
    import aap_migration.client.aap_target_client as target_mod
    import aap_migration.utils.ssrf as ssrf_mod

    monkeypatch.setattr(ssrf_mod, "reverify_execution_url_bounded", lambda url: url)
    monkeypatch.setattr(ssrf_mod, "reverify_execution_url", lambda url: url)

    if behavior == "timeout":

        async def _get(self: Any, *args: Any, **kwargs: Any) -> Any:
            raise TimeoutError("slow backend")

        async def _version(self: Any) -> str:
            raise TimeoutError("slow backend")

    elif behavior == "backend-error":

        async def _get(self: Any, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("backend boom: secret-token-xyz")

        async def _version(self: Any) -> str:
            return "v1"

    elif behavior == "ok":

        async def _get(self: Any, *args: Any, **kwargs: Any) -> Any:
            return {"ping": "pong"}

        async def _version(self: Any) -> str:
            return "2.5.0"

    else:  # pragma: no cover
        raise AssertionError(behavior)
    monkeypatch.setattr(source_mod.AAPSourceClient, "get", _get)
    monkeypatch.setattr(target_mod.AAPTargetClient, "get", _get)
    monkeypatch.setattr(source_mod.AAPSourceClient, "get_version", _version)
    monkeypatch.setattr(target_mod.AAPTargetClient, "get_version", _version)


class TestConnectionProbeMapping:
    """P2 #10: POST /connections/{id}/test maps failures by type."""

    def test_ssrf_rejection_is_400(self, client: Any, pair: Any, monkeypatch: Any) -> None:
        import aap_migration.utils.ssrf as ssrf_mod

        def _blocked(url: str) -> str:
            raise ValueError("Connection URL targets a blocked range")

        monkeypatch.setattr(ssrf_mod, "reverify_execution_url_bounded", _blocked)
        src, _tgt = pair
        resp = client.post(f"/api/v1/connections/{src['id']}/test")
        assert resp.status_code == 400

    def test_timeout_is_502(self, client: Any, pair: Any, monkeypatch: Any) -> None:
        _stub_clients(monkeypatch, "timeout")
        src, _tgt = pair
        resp = client.post(f"/api/v1/connections/{src['id']}/test")
        assert resp.status_code == 502
        assert "timed out" in resp.json()["detail"].lower()

    def test_backend_error_is_redacted_502(self, client: Any, pair: Any, monkeypatch: Any) -> None:
        _stub_clients(monkeypatch, "backend-error")
        src, _tgt = pair
        resp = client.post(f"/api/v1/connections/{src['id']}/test")
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert "secret-token-xyz" not in detail
        assert "boom" not in detail
        assert detail == "Connectivity test failed (see server logs for detail)"

    def test_success_shape(self, client: Any, pair: Any, monkeypatch: Any) -> None:
        _stub_clients(monkeypatch, "ok")
        src, _tgt = pair
        resp = client.post(f"/api/v1/connections/{src['id']}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["reachable"] is True
        assert body["version"] == "2.5.0"


class TestConfigValidateMapping:
    """P2 #10: POST /config/validate connectivity errors map the same way."""

    def test_timeout_is_502(self, client: Any, pair: Any, monkeypatch: Any) -> None:
        _stub_clients(monkeypatch, "timeout")
        resp = client.post("/api/v1/config/validate", json={"check_connectivity": True})
        assert resp.status_code == 502

    def test_backend_error_is_redacted(self, client: Any, pair: Any, monkeypatch: Any) -> None:
        _stub_clients(monkeypatch, "backend-error")
        resp = client.post("/api/v1/config/validate", json={"check_connectivity": True})
        assert resp.status_code == 502
        assert "secret-token-xyz" not in resp.json()["detail"]
