"""P2 #16 + W4: lenient 200+warning on empty DB for all readers."""

from fastapi.testclient import TestClient


def test_empty_readers_lenient(client: TestClient) -> None:
    # Fresh STARTUP_CWD (no DB) + no connections: all readers 200+warning,
    # never 400/404 (no require_connections gate on empty DB).
    show = client.get("/api/v1/state/show")
    assert show.status_code == 200, show.text
    assert show.json()["migration_id"] is None
    assert "warning" in show.json()

    mappings = client.get("/api/v1/state/mappings")
    assert mappings.status_code == 200, mappings.text
    assert mappings.json()["mappings"] == []
    assert "warning" in mappings.json()

    retry = client.get("/api/v1/retry/status")
    assert retry.status_code == 200, retry.text
    assert retry.json()["by_type"] == {}
    assert "warning" in retry.json()

    cps = client.get("/api/v1/checkpoints")
    assert cps.status_code == 200, cps.text
    assert cps.json()["checkpoints"] == []
    assert "warning" in cps.json()

    resume = client.get("/api/v1/checkpoints/resume-info")
    assert resume.status_code == 200, resume.text
    assert "warning" in resume.json()


def test_fresh_probe_migrations_status(client: TestClient) -> None:
    # Fresh probe: no DB + no pair configured -> 200+warning (not 400).
    resp = client.get("/api/v1/migrations/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["migration_id"] is None
    assert body["resource_stats"] == {}
    assert "warning" in body


def test_strict_still_404(client: TestClient) -> None:
    assert client.get("/api/v1/state/show?strict=true").status_code == 404
    assert client.get("/api/v1/migrations/status?strict=true").status_code == 404
    assert client.get("/api/v1/retry/status?strict=true").status_code == 404
    assert client.get("/api/v1/checkpoints?strict=true").status_code == 404
