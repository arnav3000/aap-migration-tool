"""Base HTTP client for AAP Bridge.

This module provides a base async HTTP client with connection pooling,
rate limiting, retry logic, and comprehensive logging.
"""

import asyncio
import time
from datetime import UTC
from functools import partial
from typing import Any, cast
from urllib.parse import ParseResult, urljoin, urlparse

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
from aap_migration.utils.ssrf import reverify_execution_url_bounded

logger = get_logger(__name__)


#: Upper bound for a parsed Retry-After delay. An HTTP-date far in the
#: future would otherwise park the single FIFO worker for hours; the
#: sleeper also caps at its own max_wait, but the parser must not produce
#: day-scale deltas in the first place.
RETRY_AFTER_MAX_SECS = 300


def _parse_retry_after(value: str | None, max_secs: int = RETRY_AFTER_MAX_SECS) -> int | None:
    """Parse a Retry-After header value defensively (never raises).

    Integer delay-seconds are used directly (capped at *max_secs*);
    otherwise an HTTP-date (RFC 7231) is converted to a non-negative delta
    against now, also capped. Any other shape returns None so callers fall
    back to default backoff.
    """
    if not value:
        return None
    try:
        return max(0, min(int(value.strip()), max_secs))
    except (TypeError, ValueError, AttributeError):
        pass
    try:
        from datetime import datetime
        from email.utils import parsedate_to_datetime

        moment = parsedate_to_datetime(value.strip())
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        delta = (moment - datetime.now(UTC)).total_seconds()
        return max(0, min(int(delta), max_secs))
    except Exception:
        return None


def _origin_tuple(parsed: ParseResult) -> tuple[str, str, int]:
    """Return the (scheme, host, port) origin of a parsed URL.

    Default ports are normalized (443/80) so an explicit ``:443`` and an
    implicit one compare equal in the manual same-origin redirect check.
    """
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), port)


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
            # Fail closed on redirects: the stored URL was validated and
            # re-verified at execution time, but a 302 to a metadata or
            # internal address would otherwise be followed automatically
            # with the bearer token attached (CWE-918). Redirects are
            # followed manually in request() only for same-origin targets
            # that pass reverify_execution_url().
            follow_redirects=False,
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
            retry_seconds = _parse_retry_after(retry_after)
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

    async def request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make an HTTP request with rate limiting and error handling.

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
            Various APIError subclasses: For API errors
        """
        # Never let a caller re-enable auto-redirects or disable TLS via
        # kwargs: the manual same-origin loop below is the only redirect
        # path, and it never forwards the bearer token cross-origin.
        if kwargs.pop("follow_redirects", False):
            logger.warning("follow_redirects kwarg ignored: manual same-origin loop enforced")
        url = self._build_url(endpoint)

        # Execution-time SSRF re-verification (fail closed). A single check
        # runs after the rate-limit sleep, immediately before the bearer
        # token is sent, against the fully built request URL -- so a rebind
        # during the sleep (or an absolute-URL endpoint to a different host)
        # cannot slip through. It bypasses the success cache so the verdict
        # always reflects fresh DNS. One check per request (not two) bounds
        # the DNS helper threads each call can strand. Private/internal IPs
        # are blocked by default; set AAP_BRIDGE_SSRF_ALLOW_PRIVATE=1 for
        # legitimate on-prem controllers.
        # Apply rate limiting
        await self._rate_limit_wait()

        try:
            # Blocking DNS re-verify must stay off the server event loop:
            # one slow hostname would otherwise stall all concurrent API
            # traffic. to_thread keeps the FIFO-worker path unchanged.
            await asyncio.to_thread(partial(reverify_execution_url_bounded, url, use_cache=False))
        except ValueError as exc:
            raise NetworkError(f"SSRF re-verification blocked request: {exc}") from exc

        # Log request payload if enabled
        if should_log_payloads(logger, self.log_payloads) and json_data is not None:
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
            # Manual same-origin redirect following (fail closed). httpx is
            # configured with follow_redirects=False so a validated URL
            # answering with a cross-host 302 cannot pull the bearer token
            # to an unvalidated target. Same-origin redirects that pass
            # reverify_execution_url() are followed (bounded); anything
            # else fails instead of leaking the request.
            seen_targets: set[str] = {url}
            for _ in range(3):
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get("location")
                if not location:
                    break
                target = (
                    urljoin(url, location)
                    if not location.startswith(("http://", "https://"))
                    else location
                )
                if urlparse(target).scheme not in ("http", "https"):
                    raise NetworkError(f"Redirect to unsupported scheme blocked: {target}")
                base_origin = urlparse(self.base_url)
                target_origin = urlparse(target)

                if _origin_tuple(base_origin) != _origin_tuple(target_origin):
                    raise NetworkError(
                        "Cross-origin redirect blocked for AAP client "
                        f"({target_origin.hostname}); failing closed."
                    )
                if target in seen_targets:
                    raise NetworkError(f"Cyclic redirect blocked: {target}")
                seen_targets.add(target)
                try:
                    # Bounded reverify (10s cap) off the event loop so a
                    # poison redirect target cannot stall API traffic.
                    # Bypass the success cache: the target was never checked.
                    await asyncio.to_thread(
                        partial(reverify_execution_url_bounded, target, use_cache=False)
                    )
                except ValueError as exc:
                    raise NetworkError(f"Redirect target blocked: {exc}") from exc
                # Browser/fetch semantics: 301/302 on POST become GET
                # without a body; 307/308 preserve method+body; 303 is
                # always GET without a body. Query params are always
                # forwarded (the pre-redirect request already carried
                # them; dropping them silently un-filters list calls).
                if response.status_code == 303:
                    follow_method = "GET"
                elif response.status_code in (301, 302) and method == "POST":
                    follow_method = "GET"
                else:
                    follow_method = method
                follow_json = None if follow_method == "GET" else json_data
                response = await self.client.request(
                    method=follow_method,
                    url=target,
                    params=params,
                    json=follow_json,
                    **kwargs,
                )
                url = target
            if response.status_code in (301, 302, 303, 307, 308):
                raise NetworkError(
                    f"Redirect chain exceeded 3 hops or terminated at {url}; failing closed."
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

            # Handle errors
            if response.status_code >= 400:
                self._handle_error_response(response)

            # Return JSON response
            return cast(dict[str, Any], response.json()) if response.text else {}

        except httpx.NetworkError as e:
            logger.error("network_error", method=method, url=url, error=str(e))
            raise NetworkError(f"Network error: {str(e)}") from e
        except httpx.TimeoutException as e:
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
            # Re-raise our custom exceptions
            raise
        except Exception as e:
            logger.error("unexpected_error", method=method, url=url, error=str(e), exc_info=True)
            raise

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
