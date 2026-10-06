"""Auth wiring tests: every shipped route fails closed without a token (#6).

Health/readiness are intentionally keyless (orchestrator liveness probes
rarely carry API keys; gating them restarts a healthy server): all other
routes of the four shipped routers (plus docs/discovery) must 401 without
a key and serve with one.

The shared fixture opts into ``AAP_BRIDGE_ALLOW_ANON=1`` so most tests can
exercise routes without auth. These tests build the opposite posture: with
a token configured and no anonymous opt-in, every authed route must 401
without a key and serve with one.
"""

from typing import Any

import pytest


@pytest.fixture()
def locked_down(client: Any, monkeypatch: Any) -> Any:
    """Revoke the fixture's anonymous opt-in and configure a token."""
    monkeypatch.delenv("AAP_BRIDGE_ALLOW_ANON", raising=False)
    monkeypatch.setenv("AAP_BRIDGE_API_TOKEN", "test-token-0123456789abcdef")
    return client


class TestFailClosedRoutes:
    """P1 #6: dropping auth from a router must break these tests."""

    _GETS = [
        "/api/v1/version",
        "/api/v1/resources",
        "/api/v1/jobs",
        "/api/v1/connections",
        "/api/v1/connections/active",
    ]

    # Health/readiness are keyless liveness probes (see app.create_app):
    # they must serve without a key even in the locked-down posture.
    _KEYLESS_GETS = [
        "/api/v1/health",
        "/api/v1/ready",
    ]

    @pytest.mark.parametrize("path", _GETS)
    def test_unauthenticated_gets_are_401(self, locked_down: Any, path: str) -> None:
        assert locked_down.get(path).status_code == 401

    @pytest.mark.parametrize("path", _KEYLESS_GETS)
    def test_health_ready_are_keyless(self, locked_down: Any, path: str) -> None:
        assert locked_down.get(path).status_code == 200

    def test_unauthenticated_unknown_job_is_401_not_404(self, locked_down: Any) -> None:
        # Auth runs before the handler: a missing key beats a missing job.
        assert locked_down.get("/api/v1/jobs/does-not-exist").status_code == 401

    def test_docs_paths_require_auth(self, locked_down: Any) -> None:
        for path in ("/api/v1/docs", "/api/v1/redoc", "/api/v1/openapi.json"):
            assert locked_down.get(path).status_code == 401

    def test_authenticated_gets_serve(self, locked_down: Any) -> None:
        headers = {"X-API-Key": "test-token-0123456789abcdef"}
        assert locked_down.get("/api/v1/health", headers=headers).status_code == 200
        assert locked_down.get("/api/v1/version", headers=headers).status_code == 200
        assert locked_down.get("/api/v1/jobs", headers=headers).status_code == 200

    def test_every_route_carries_auth_dependency(self, locked_down: Any) -> None:
        from aap_migration.api.app import create_app

        # Structural backstop: a future router included without
        # dependencies=[Depends(require_api_key)] fails here, not in prod.
        # Health/readiness are the intentional keyless exception
        # (orchestrator probes); version/resources carry per-route auth.
        app = create_app()
        unguarded = []
        for route in app.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/api/v1/"):
                continue
            if path in ("/api/v1/health", "/api/v1/ready"):
                continue
            deps = getattr(route, "dependencies", None)
            if not deps:
                unguarded.append(path)
        assert unguarded == [], unguarded
