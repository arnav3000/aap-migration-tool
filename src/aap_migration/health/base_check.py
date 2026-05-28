"""Base class for health checks."""

from abc import ABC, abstractmethod
from typing import Any

from aap_migration.client.aap_client import AAPClient
from aap_migration.health.models import HealthCheckResult


class BaseHealthCheck(ABC):
    """Base class for all health checks."""

    def __init__(self, client: AAPClient):
        """Initialize health check.

        Args:
            client: AAP client for API calls
        """
        self.client = client

    @property
    @abstractmethod
    def check_name(self) -> str:
        """Name of the health check."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what this check validates."""
        pass

    @abstractmethod
    async def run(self) -> HealthCheckResult:
        """Execute the health check.

        Returns:
            HealthCheckResult with findings
        """
        pass

    async def _fetch_resources(
        self,
        resource_type: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch resources from AAP API with pagination.

        Args:
            resource_type: Resource type endpoint (e.g., "job_templates")
            params: Optional query parameters

        Returns:
            List of resources
        """
        all_results = []
        params = params or {}
        endpoint = f"{resource_type}/"

        while endpoint:
            response = await self.client.get(endpoint, params=params)
            results = response.get("results", [])
            all_results.extend(results)

            # Handle pagination
            next_url = response.get("next")
            if next_url:
                # Extract relative path from next URL
                from urllib.parse import urlparse

                parsed = urlparse(next_url)
                # Handle both AAP 2.4 (/api/v2/) and AAP 2.6 (/api/controller/v2/)
                endpoint = (
                    parsed.path.replace("/api/controller/v2/", "")
                    .replace("/api/v2/", "")
                    .lstrip("/")
                )
                params = {}  # Params are in the next URL
            else:
                endpoint = None

        return all_results
