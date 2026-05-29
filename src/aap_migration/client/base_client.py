"""Base HTTP client for AAP Bridge.

This module provides a base async HTTP client with connection pooling,
rate limiting, retry logic, and comprehensive logging.
"""

import asyncio
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from aap_migration.client.exceptions import (
    APIError,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NetworkError,
    NotFoundError,
    PendingDeletionError,
    RateLimitError,
    ResourceInUseError,
    ServerError,
)
from aap_migration.utils.logging import (
    get_logger,
    log_api_request,
    sanitize_payload,
    should_log_payloads,
    truncate_payload,
)

logger = get_logger(__name__)


class BaseAPIClient:
    """Base async HTTP client with retry logic and rate limiting.

    This client provides:
    - Connection pooling
    - Rate limiting
    - Request/response logging
    - Automatic retry for transient failures
    - Proper error handling and exception mapping
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        verify_ssl: bool = True,
        timeout: int = 30,
        rate_limit: int = 20,
        max_connections: int | None = None,
        max_keepalive_connections: int | None = None,
        log_payloads: bool = False,
        max_payload_size: int = 10000,
    ):
        """Initialize base API client.

        Args:
            base_url: Base URL for API requests
            token: Authentication token
            verify_ssl: Whether to verify SSL certificates
            timeout: Request timeout in seconds
            rate_limit: Maximum requests per second
            max_connections: Maximum number of connections in pool (default: 50)
            max_keepalive_connections: Maximum keep-alive connections (default: 20)
            log_payloads: Enable request/response payload logging at DEBUG level
            max_payload_size: Maximum payload size (chars) to log before truncation
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.verify_ssl = verify_ssl

        # Payload logging configuration
        self.log_payloads = log_payloads
        self.max_payload_size = max_payload_size

        # Rate limiting
        self.rate_limit = rate_limit
        self._rate_limit_lock = asyncio.Lock()
        self._last_request_time: float = 0
        self._min_request_interval = 1.0 / rate_limit if rate_limit > 0 else 0

        # Set defaults if not provided
        if max_connections is None:
            max_connections = 50
        if max_keepalive_connections is None:
            max_keepalive_connections = 20

        # Create async HTTP client with connection pooling
        self.client = httpx.AsyncClient(
            headers=self._build_headers(),
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
            ),
            verify=verify_ssl,
            follow_redirects=True,
        )

        logger.info(
            "client_initialized",
            base_url=self.base_url,
            rate_limit=rate_limit,
            max_connections=max_connections,
        )

    def _build_headers(self) -> dict[str, str]:
        """Build HTTP headers for requests.

        Returns:
            Dictionary of HTTP headers
        """
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _build_url(self, endpoint: str) -> str:
        """Build full URL from endpoint.

        Args:
            endpoint: API endpoint path

        Returns:
            Full URL
        """
        # Remove leading slash if present
        endpoint = endpoint.lstrip("/")
        return urljoin(f"{self.base_url}/", endpoint)

    async def _rate_limit_wait(self) -> None:
        """Implement rate limiting by waiting if necessary."""
        if self._min_request_interval > 0:
            async with self._rate_limit_lock:
                now = time.time()
                time_since_last = now - self._last_request_time

                if time_since_last < self._min_request_interval:
                    wait_time = self._min_request_interval - time_since_last
                    await asyncio.sleep(wait_time)

                self._last_request_time = time.time()

    def _handle_error_response(self, response: httpx.Response) -> None:
        """Handle error responses by raising appropriate exceptions.

        Args:
            response: HTTP response object

        Raises:
            AuthenticationError: For 401 responses
            AuthorizationError: For 403 responses
            NotFoundError: For 404 responses
            ConflictError: For 409 responses
            RateLimitError: For 429 responses
            ServerError: For 5xx responses
            APIError: For other error responses
        """
        status_code = response.status_code

        # Try to parse error response
        try:
            error_data = response.json()
        except Exception:
            error_data = {"detail": response.text}

        # Handle case where API returns a list instead of dict
        if isinstance(error_data, list):
            # Convert list to string representation for error message
            error_message = (
                ", ".join(str(item) for item in error_data) if error_data else "Unknown error"
            )
            # Wrap in dict for consistent error_data structure
            error_data = {"detail": error_message, "_raw_list": error_data}
        else:
            error_message = error_data.get("detail", error_data.get("message", "Unknown error"))

        # Map status codes to exceptions
        if status_code == 401:
            raise AuthenticationError(
                message="Authentication failed", status_code=status_code, response=error_data
            )
        elif status_code == 403:
            raise AuthorizationError(
                message="Authorization failed", status_code=status_code, response=error_data
            )
        elif status_code == 404:
            raise NotFoundError(
                message="Resource not found", status_code=status_code, response=error_data
            )
        elif status_code == 409:
            # Parse 409 errors to detect specific conflict types
            error_detail = error_message.lower() if isinstance(error_message, str) else ""

            # Check for "already pending deletion" (idempotent success)
            if "already pending deletion" in error_detail or "pending deletion" in error_detail:
                raise PendingDeletionError(
                    message=error_message,
                    status_code=status_code,
                    response=error_data,
                )

            # Check for "resource is being used by running jobs"
            if (
                "being used" in error_detail
                or "active jobs" in error_detail
                or "running jobs" in error_detail
            ):
                # Try to extract active_jobs list from response
                active_jobs = (
                    error_data.get("active_jobs", []) if isinstance(error_data, dict) else []
                )
                raise ResourceInUseError(
                    message=error_message,
                    status_code=status_code,
                    response=error_data,
                    active_jobs=active_jobs,
                )

            # Generic 409 conflict (e.g., "resource already exists")
            raise ConflictError(
                message="Resource conflict (may already exist)",
                status_code=status_code,
                response=error_data,
            )
        elif status_code == 429:
            retry_after = response.headers.get("Retry-After")
            retry_seconds = int(retry_after) if retry_after else None
            raise RateLimitError(
                message="Rate limit exceeded",
                status_code=status_code,
                response=error_data,
                retry_after=retry_seconds,
            )
        elif 500 <= status_code < 600:
            raise ServerError(
                message=f"Server error: {error_message}",
                status_code=status_code,
                response=error_data,
            )
        else:
            raise APIError(
                message=f"API error: {error_message}",
                status_code=status_code,
                response=error_data,
            )

    # Maximum retries for transient errors (429 rate limit, 5xx server errors)
    MAX_RETRIES = 5
    # Initial backoff delay in seconds (doubles on each retry)
    INITIAL_RETRY_DELAY = 1.0

    async def request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make an HTTP request with rate limiting, retry, and error handling.

        Automatically retries on:
        - HTTP 429 (Too Many Requests): Uses Retry-After header if present,
          otherwise exponential backoff starting at 1 second.
        - HTTP 5xx (Server Errors): Exponential backoff for transient failures.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, etc.)
            endpoint: API endpoint path
            params: Query parameters
            json_data: JSON request body
            **kwargs: Additional arguments passed to httpx

        Returns:
            Response JSON data

        Raises:
            NetworkError: For network-related errors
            RateLimitError: After exhausting retries on 429 responses
            ServerError: After exhausting retries on 5xx responses
            Various APIError subclasses: For non-retryable API errors
        """
        url = self._build_url(endpoint)
        retry_delay = self.INITIAL_RETRY_DELAY
        last_exception: Exception | None = None

        for attempt in range(self.MAX_RETRIES + 1):
            # Apply rate limiting
            await self._rate_limit_wait()

            # Log request payload if enabled (only on first attempt)
            if (
                attempt == 0
                and should_log_payloads(logger, self.log_payloads)
                and json_data is not None
            ):
                sanitized_request = sanitize_payload(json_data)
                payload_str = truncate_payload(sanitized_request, self.max_payload_size)
                logger.debug(
                    "api_request_payload",
                    method=method,
                    url=url,
                    payload=payload_str,
                    payload_size=len(str(json_data)),
                )

            # Track request timing
            start_time = time.time()

            try:
                response = await self.client.request(
                    method=method, url=url, params=params, json=json_data, **kwargs
                )

                duration_ms = (time.time() - start_time) * 1000

                # Log request
                log_api_request(
                    logger,
                    method=method,
                    url=url,
                    status_code=response.status_code,
                    duration_ms=duration_ms,
                )

                # Log response payload if enabled
                if should_log_payloads(logger, self.log_payloads) and response.text:
                    try:
                        response_data = response.json()
                        sanitized_response = sanitize_payload(response_data)
                        payload_str = truncate_payload(sanitized_response, self.max_payload_size)
                        logger.debug(
                            "api_response_payload",
                            method=method,
                            url=url,
                            status_code=response.status_code,
                            payload=payload_str,
                            payload_size=len(response.text),
                        )
                    except Exception:
                        # If JSON parsing fails, log as text (truncated)
                        logger.debug(
                            "api_response_payload",
                            method=method,
                            url=url,
                            status_code=response.status_code,
                            payload=response.text[: self.max_payload_size],
                            payload_size=len(response.text),
                        )

                # Handle retryable errors (429 and 5xx)
                if response.status_code == 429 and attempt < self.MAX_RETRIES:
                    retry_after = response.headers.get("Retry-After")
                    wait_time = int(retry_after) if retry_after else retry_delay
                    logger.warning(
                        "rate_limited_retrying",
                        method=method,
                        endpoint=endpoint,
                        attempt=attempt + 1,
                        max_retries=self.MAX_RETRIES,
                        retry_after=wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    retry_delay = min(retry_delay * 2, 60)  # Cap at 60s
                    continue

                if 500 <= response.status_code < 600 and attempt < self.MAX_RETRIES:
                    logger.warning(
                        "server_error_retrying",
                        method=method,
                        endpoint=endpoint,
                        status_code=response.status_code,
                        attempt=attempt + 1,
                        max_retries=self.MAX_RETRIES,
                        retry_after=retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)  # Cap at 60s
                    continue

                # Handle non-retryable errors (or final retry attempt)
                if response.status_code >= 400:
                    self._handle_error_response(response)

                # Return JSON response
                return response.json() if response.text else {}

            except httpx.NetworkError as e:
                if attempt < self.MAX_RETRIES:
                    logger.warning(
                        "network_error_retrying",
                        method=method,
                        url=url,
                        error=str(e),
                        attempt=attempt + 1,
                        max_retries=self.MAX_RETRIES,
                        retry_after=retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
                    last_exception = e
                    continue
                logger.error("network_error", method=method, url=url, error=str(e))
                raise NetworkError(f"Network error: {str(e)}") from e
            except httpx.TimeoutException as e:
                if attempt < self.MAX_RETRIES:
                    logger.warning(
                        "timeout_error_retrying",
                        method=method,
                        url=url,
                        error=str(e),
                        attempt=attempt + 1,
                        max_retries=self.MAX_RETRIES,
                        retry_after=retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
                    last_exception = e
                    continue
                logger.error("timeout_error", method=method, url=url, error=str(e))
                raise NetworkError(f"Request timeout: {str(e)}") from e
            except (
                AuthenticationError,
                AuthorizationError,
                NotFoundError,
                ConflictError,
                RateLimitError,
                ServerError,
                APIError,
            ):
                # Re-raise our custom exceptions (non-retryable at this point)
                raise
            except Exception as e:
                logger.error(
                    "unexpected_error", method=method, url=url, error=str(e), exc_info=True
                )
                raise

        # Should not reach here, but safety net
        if last_exception:
            raise NetworkError(
                f"Request failed after {self.MAX_RETRIES} retries: {str(last_exception)}"
            ) from last_exception
        raise APIError(f"Request failed after {self.MAX_RETRIES} retries")

    async def get(
        self, endpoint: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Make a GET request.

        Args:
            endpoint: API endpoint path
            params: Query parameters
            **kwargs: Additional arguments

        Returns:
            Response JSON data
        """
        return await self.request("GET", endpoint, params=params, **kwargs)

    async def post(
        self,
        endpoint: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make a POST request.

        Args:
            endpoint: API endpoint path
            json_data: JSON request body
            params: Query parameters
            **kwargs: Additional arguments

        Returns:
            Response JSON data
        """
        return await self.request("POST", endpoint, params=params, json_data=json_data, **kwargs)

    async def put(
        self,
        endpoint: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make a PUT request.

        Args:
            endpoint: API endpoint path
            json_data: JSON request body
            params: Query parameters
            **kwargs: Additional arguments

        Returns:
            Response JSON data
        """
        return await self.request("PUT", endpoint, params=params, json_data=json_data, **kwargs)

    async def patch(
        self,
        endpoint: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make a PATCH request.

        Args:
            endpoint: API endpoint path
            json_data: JSON request body
            params: Query parameters
            **kwargs: Additional arguments

        Returns:
            Response JSON data
        """
        return await self.request("PATCH", endpoint, params=params, json_data=json_data, **kwargs)

    async def delete(
        self, endpoint: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Make a DELETE request.

        Args:
            endpoint: API endpoint path
            params: Query parameters
            **kwargs: Additional arguments

        Returns:
            Response JSON data
        """
        return await self.request("DELETE", endpoint, params=params, **kwargs)

    async def options(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        suppress_server_error: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make an OPTIONS request.

        Used for schema discovery and CORS preflight requests.

        Args:
            endpoint: API endpoint path
            params: Query parameters
            suppress_server_error: If True, don't log server errors (caller handles them)
            **kwargs: Additional arguments

        Returns:
            Response JSON data
        """
        # suppress_server_error is handled here, not passed to httpx
        # The caller (schema_generator) handles 500 errors gracefully
        _ = suppress_server_error  # Acknowledged but error handling is in caller
        return await self.request("OPTIONS", endpoint, params=params, **kwargs)

    async def close(self) -> None:
        """Close the HTTP client and clean up resources."""
        await self.client.aclose()
        logger.info("client_closed", base_url=self.base_url)

    async def __aenter__(self) -> "BaseAPIClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()
