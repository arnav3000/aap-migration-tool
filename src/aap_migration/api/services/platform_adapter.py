from typing import Any, cast

import httpx

from aap_migration.api.models import Connection
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.engine_adapter import connection_to_aap_config


class PlatformAdapter:
    """Lightweight HTTP adapter for platform resource browsing.

    URL and auth align with ``ConnectionService`` / ``connection_to_aap_config``
    (single api_prefix, decrypted token, connection timeout).
    """

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        config = connection_to_aap_config(conn)
        self.base_url = config.url.rstrip("/")
        self.verify_ssl = config.verify_ssl
        self.timeout = config.timeout
        self.headers: dict[str, str] = {}
        if config.token:
            scheme = ConnectionService._auth_scheme(conn)
            self.headers["Authorization"] = f"{scheme} {config.token}"

    def _get(self, path: str, params: dict | None = None) -> dict[Any, Any]:
        resp = httpx.get(
            f"{self.base_url}{path}",
            headers=self.headers,
            params=params,
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return cast(dict[Any, Any], resp.json())

    def discover_resource_types(self) -> list[dict]:
        try:
            data = self._get("/")
            if not isinstance(data, dict):
                return []
            return [
                {"name": key, "label": key.replace("_", " ").title(), "api_path": path}
                for key, path in sorted(data.items())
                if isinstance(path, str)
            ]
        except Exception:
            return []

    def fetch_all(self, resource_type: str) -> list[dict]:
        results = []
        page = 1
        while True:
            try:
                data = self._get(f"/{resource_type}/", params={"page": page, "page_size": 200})
                results.extend(data.get("results", []))
                if not data.get("next"):
                    break
                page += 1
            except Exception:
                break
        return results

    def list_resources(self, resource_type: str, page: int, page_size: int, search: str) -> dict:
        params: dict = {"page": page, "page_size": page_size}
        if search:
            params["search"] = search
        try:
            data = self._get(f"/{resource_type}/", params=params)
            return {
                "count": data.get("count", 0),
                "results": data.get("results", []),
                "page": page,
                "page_size": page_size,
            }
        except Exception as e:
            return {
                "count": 0,
                "results": [],
                "page": page,
                "page_size": page_size,
                "error": str(e),
            }
