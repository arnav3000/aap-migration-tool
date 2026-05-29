"""Base class for health checks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from aap_migration.client.aap_client import AAPClient
from aap_migration.health.models import HealthCheckResult

if TYPE_CHECKING:
    from aap_migration.health.checker import HealthChecker


class BaseHealthCheck(ABC):
    """Base class for all health checks."""

    def __init__(
        self, client: AAPClient, checker: HealthChecker | None = None
    ):
        """Initialize health check.

        Args:
            client: AAP client for API calls
            checker: Optional HealthChecker instance for shared resource cache.
                     When provided, _fetch_resources() uses the checker's cache
                     to avoid redundant API calls across checks.
        """
        self.client = client
        self.checker = checker

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

        When a checker instance is available and no custom params are provided,
        resources are fetched through the checker's shared cache to avoid
        redundant API calls across checks (e.g., job_templates fetched once
        instead of 5 times).

        Args:
            resource_type: Resource type endpoint (e.g., "job_templates")
            params: Optional query parameters. If provided, cache is bypassed
                    since custom params may return different result sets.

        Returns:
            List of resources
        """
        # Use checker's shared cache when available and no custom params
        if self.checker and not params:
            return await self.checker.get_cached_resources(resource_type)

        # Direct fetch (standalone usage or custom params)
        return await self._fetch_resources_direct(resource_type, params)

    async def _fetch_resources_direct(
        self,
        resource_type: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch resources directly from AAP API with pagination.

        This method always makes API calls without caching. Used internally
        by the checker's cache and as fallback for standalone usage.

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
