"""REST API tests: system endpoints + OpenAPI parity."""

from typing import Any

from fastapi.testclient import TestClient


class TestSystem:
    def test_health(self, client: TestClient) -> None:
        body = client.get("/api/v1/health").json()
        assert body["status"] == "ok"
        assert body["worker"] == "alive"
        assert "queue_depth" in body

    def test_version(self, client: TestClient) -> None:
        body = client.get("/api/v1/version").json()
        assert body["prog_name"] == "aap-bridge"

    def test_resources(self, client: TestClient) -> None:
        body = client.get("/api/v1/resources").json()
        assert "organizations" in body["all"]
        assert body["resources"]["hosts"]["has_exporter"] is True

    def test_resource_detail(self, client: TestClient) -> None:
        assert client.get("/api/v1/resources/hosts").status_code == 200
        assert client.get("/api/v1/resources/nope").status_code == 404


class TestOpenApiParity:
    """Every v1.x CLI capability must have at least one endpoint.

    The CLI command tree is the source of truth: if a CLI command is added
    without an API mapping, this test fails and forces the mapping update.
    """

    CLI_TO_API = {
        "migrate": "/api/v1/migrations",
        "export": "/api/v1/exports",
        "transform": "/api/v1/transforms",
        "import": "/api/v1/imports",
        "patch-projects": "/api/v1/imports/patch-projects",
        "credentials": "/api/v1/credentials/compare",
        "iam": "/api/v1/iam/audit",
        "validate": "/api/v1/validations",
        "analyze-dependencies": "/api/v1/analysis/dependencies",
        "migration-report": "/api/v1/reports/migration",
        "enhanced-report": "/api/v1/reports/enhanced",
        "analyze-project-failures": "/api/v1/reports/project-failures",
        "state": "/api/v1/state/show",
        "retry": "/api/v1/retry/failed",
        "prep": "/api/v1/prep",
        "cleanup": "/api/v1/cleanup",
        "config": "/api/v1/config/validate",
    }

    def test_cli_commands_mapped(self) -> None:
        from aap_migration.cli.main import cli

        cli_names = set(cli.commands.keys())
        unmapped = {name for name in cli_names if name not in self.CLI_TO_API}
        # Allowlist for CLI-only UX that intentionally has no REST equivalent.
        allowed = {"menu", "completion", "help"}
        assert not (unmapped - allowed), f"CLI commands without API mapping: {unmapped - allowed}"

    def test_all_paths_present(self, client: TestClient) -> None:
        spec = client.get("/openapi.json").json()
        paths = set(spec["paths"])
        missing = [p for p in self.CLI_TO_API.values() if p not in paths]
        assert not missing, f"missing endpoints: {missing}"

    def test_mapped_paths_expose_expected_methods(self, client: TestClient) -> None:
        spec = client.get("/openapi.json").json()["paths"]
        expected_post = [
            "/api/v1/migrations",
            "/api/v1/exports",
            "/api/v1/transforms",
            "/api/v1/imports",
            "/api/v1/validations",
            "/api/v1/prep",
            "/api/v1/cleanup",
            "/api/v1/iam/audit",
            "/api/v1/credentials/compare",
            "/api/v1/reports/migration",
            "/api/v1/retry/failed",
            "/api/v1/state/export",
        ]
        for path in expected_post:
            assert "post" in spec[path], f"{path} must accept POST"
        assert "get" in spec["/api/v1/state/show"]
        assert "get" in spec["/api/v1/jobs"]

    def test_connection_create_requires_fields(self, client: TestClient) -> None:
        # Required request fields (not just paths) are part of the contract.
        resp = client.post("/api/v1/connections", json={"name": "x"})
        assert resp.status_code == 422
        assert isinstance(resp.json()["detail"], str)


class TestReadinessBranches:
    def test_ready_503_branches_and_none_sentinels(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        """Every 503 branch plus the None-instead-of--1 sentinel shape."""

        # Missing DB file -> 503 with the missing marker.
        monkeypatch.setenv("AAP_BRIDGE_API_DB", "/tmp/definitely-missing-api.db")
        missing = client.get("/api/v1/ready")
        assert missing.status_code == 503, missing.text
        assert "unwritable: missing" in missing.json()["checks"]["database"]

        # Unwritable job dir (probe raises) -> 503.
        import aap_migration.api.routers.system as system_mod

        def _boom(directory: str) -> None:
            raise OSError("denied")

        monkeypatch.setattr(system_mod, "_probe_dir_writable", _boom)
        denied = client.get("/api/v1/ready")
        assert denied.status_code == 503, denied.text
        assert "denied" in denied.json()["checks"]["job_dir"]

    def test_health_unknown_manager_none_sentinels(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        """No manager reads as unknown with None (never -1) numerics."""
        import aap_migration.api.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "_manager", None)
        monkeypatch.setattr(jobs_mod, "manager_or_none", lambda: None)
        body = client.get("/api/v1/health").json()
        assert body["worker"] == "unknown"
        assert body["queue_depth"] is None
        assert body["orphans"] is None
        assert body["fenced_dirs"] is None
