"""P2 #18, #12+#13, #15, #27: shapes, deprecations, models, phases."""

from typing import Any

from api_shared import _fake_success
from fastapi.testclient import TestClient


def test_valid_bool_not_literal(pair: Any, client: TestClient) -> None:
    body = client.post("/api/v1/config/validate", json={}).json()
    assert body["valid"] is True
    assert isinstance(body["valid"], bool)
    # OpenAPI pins bool (not Literal[True] const).
    spec = client.get("/api/v1/openapi.json").json()
    cfg = spec["components"]["schemas"]["ConfigValidateOut"]["properties"]["valid"]
    assert cfg.get("type") == "boolean"
    assert "const" not in cfg and cfg.get("enum") != [True]
    conn = spec["components"]["schemas"]["ConnectionTestOut"]["properties"]["reachable"]
    assert conn.get("type") == "boolean"


def test_errors_alias_removed(client: TestClient) -> None:
    body = client.get("/api/v1/prep/schemas").json()
    assert "errors_by_file" in body and "errors" not in body
    assert isinstance(body["errors_by_file"], dict)
    # Validation results elsewhere keep errors as list[str] under their own
    # models; the prep route no longer serves the colliding dict alias.
    spec = client.get("/api/v1/openapi.json").json()
    props = spec["components"]["schemas"]["PrepSchemasOut"]["properties"]
    assert "errors" not in props
    assert "errors_by_file" in props
    # Total alias same treatment.
    arts = spec["components"]["schemas"]["JobArtifactsOut"]["properties"]
    assert arts["total"].get("deprecated") is True
    assert "removed in v2" in (arts["total"].get("description") or "")


def test_sync_models_constrain_shape(pair: Any, client: TestClient, monkeypatch: Any) -> None:
    # P2 #15: models only constrain/shape, payloads identical.
    show = client.get("/api/v1/state/show").json()
    assert set(show) >= {"migration_id", "stats"}
    jobs = client.get("/api/v1/jobs").json()
    assert set(jobs) >= {"items", "total", "limit", "offset"}
    res = client.get("/api/v1/resources").json()
    assert set(res) >= {"all", "fully_supported", "migration_order", "cleanup_order", "resources"}
    # Response models are wired (OpenAPI success schemas, not bare dict).
    spec = client.get("/api/v1/openapi.json").json()
    assert "StateShowOut" in spec["components"]["schemas"]
    assert "JobListOut" in spec["components"]["schemas"]
    assert "ResourcesOut" in spec["components"]["schemas"]


def test_phase_unification(pair: Any, client: TestClient, monkeypatch: Any) -> None:
    from aap_migration.api.schemas import ImportRequest, MigrateRequest

    # Shared superset type present.
    assert set(ImportRequest.model_fields["phase"].annotation.__args__) == {
        "phase1",
        "phase2",
        "phase3",
        "all",
    }
    assert set(MigrateRequest.model_fields["phase"].annotation.__args__) == {
        "phase1",
        "phase2",
        "phase3",
        "all",
    }
    # Per-command validation: migrate rejects phase3, import accepts it.
    assert client.post("/api/v1/migrations", json={"phase": "phase3"}).status_code == 422
    _fake_success(monkeypatch, "run_import")
    resp = client.post("/api/v1/imports", json={"phase": "phase3"})
    assert resp.status_code == 202, resp.text
    # Case-insensitive parity with CLI Choice(case_sensitive=False).
    _fake_success(monkeypatch, "run_migrate")
    resp2 = client.post("/api/v1/migrations", json={"phase": "PHASE1"})
    assert resp2.status_code == 202, resp2.text
