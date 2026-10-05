"""Leaf SSRF/URL validators shared by transport and API layers.

Single home for connection-URL safety checks (metadata-endpoint
blocking, userinfo/scheme rejection, execution-time re-verification).
Both :mod:`aap_migration.client.base_client` (low-level transport) and
:mod:`aap_migration.api.security` (API layer) import from here so the
dependency points at a leaf instead of transport importing the API
package. Pure URL/DNS checks with no security-state coupling.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import urlparse

_METADATA_HOSTNAMES = {"metadata.google.internal", "metadata.goog"}
_METADATA_V4 = ipaddress.ip_network("169.254.0.0/16")
_METADATA_V4_EXTRA = {"100.100.100.200"}
# Link-local only. ULA (fd00::/8) is private address space, not a cloud
# metadata endpoint -- it is handled by the private-address gate in
# validate/reverify (fail closed by default), not by metadata matching.
_METADATA_V6 = (ipaddress.ip_network("fe80::/10"),)

# Short-TTL cache of successful execution-time reverifications, keyed by
# normalized host. Only successes are cached; failures always fail closed
# and are never cached. TTL is intentionally short so a rebind is picked
# up quickly while healthy hosts avoid per-request DNS thread creation.
_REVERIFY_CACHE_TTL_SECS = 30.0
_REVERIFY_CACHE: dict[str, tuple[float, str]] = {}
_REVERIFY_CACHE_LOCK = threading.Lock()

_T = TypeVar("_T")

#: Bound on concurrent blocking DNS resolutions. Every timed-out check
#: abandons a daemon thread stuck in ``getaddrinfo``; without a bound, one
#: slow/poisoned hostname under parallel export fans out into hundreds of
#: lingering threads. Refusal fails closed immediately.
_DNS_SEMAPHORE = threading.Semaphore(32)


def _normalize_host(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.rstrip(".")


def _safe_host_for_log(url: str) -> str:
    """Return a redacted host for log lines (no userinfo, no token)."""
    try:
        return urlparse(url).hostname or "unknown"
    except Exception:
        return "unknown"


def _ip_is_metadata(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _ip_is_metadata(ip.ipv4_mapped)
        return any(ip in net for net in _METADATA_V6)
    if str(ip) in _METADATA_V4_EXTRA:
        return True
    return ip in _METADATA_V4


def _run_blocking_bounded(func: Callable[[], _T], timeout_secs: float) -> _T:  # noqa: UP047
    """Run a blocking *func* on a daemon thread with a timeout (fail-closed).

    Uses a plain ``threading.Thread(daemon=True)`` plus ``join(timeout)``
    -- no executor, no global ``threading.Thread`` swap, no private
    ``Executor._adjust_thread_count`` use. A timeout leaves the hung daemon
    thread to exit on its own; the caller fails fast. Raises
    ``TimeoutError`` on timeout (callers convert to ``ValueError``) and
    re-raises any exception *func* raised.
    """
    outcome: dict[str, Any] = {}

    def _target() -> None:
        try:
            outcome["value"] = func()
        except BaseException as exc:  # noqa: BLE001 -- re-raised below
            outcome["error"] = exc

    if not _DNS_SEMAPHORE.acquire(blocking=False):
        raise TimeoutError("too many concurrent DNS resolutions; failing closed")
    try:
        worker = threading.Thread(target=_target, daemon=True, name="ssrf-dns")
        worker.start()
        worker.join(timeout=max(timeout_secs, 1.0))
        if worker.is_alive():
            raise TimeoutError(f"DNS lookup timed out after {max(timeout_secs, 1.0):g}s")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]  # type: ignore[no-any-return]
    finally:
        _DNS_SEMAPHORE.release()


def _is_blocked_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for non-public IPs that must not receive bearer tokens by default."""
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _private_allowed_by_env() -> bool:
    """Explicit opt-in escape hatch for legitimate on-prem private controllers.

    ``AAP_BRIDGE_SSRF_ALLOW_PRIVATE=1`` restores the pre-hardening behavior
    of allowing RFC1918/loopback/link-local targets. Metadata endpoints are
    always blocked regardless of this flag. The legacy
    ``AAP_BRIDGE_SSRF_STRICT=1`` forces blocking (kept for compatibility).
    """
    if os.environ.get("AAP_BRIDGE_SSRF_STRICT", "").strip().lower() in {"1", "true", "yes"}:
        return False
    return os.environ.get("AAP_BRIDGE_SSRF_ALLOW_PRIVATE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def is_metadata_url(url: str) -> bool:
    """Return True when *url* targets cloud instance-metadata endpoints.

    Matches literal hostnames, IPv4/IPv6 literals (including bracketed and
    IPv4-mapped IPv6 forms), decimal/octal/hex IPv4 spellings, and the
    metadata ranges 169.254.0.0/16, fe80::/10, plus 100.100.100.200.
    DNS names are NOT resolved here (no network I/O at validation import
    time); use validate_connection_url for resolved-IP checks.
    DNS rebinding between validation and worker fetch remains a documented
    limitation: workers should re-verify or egress-filter at fetch time.
    """
    try:
        raw_host = urlparse(url).hostname or ""
    except Exception:
        return False
    host = _normalize_host(raw_host)
    if not host:
        return False
    if host in _METADATA_HOSTNAMES or host in {"169.254.169.254"}:
        return True
    try:
        return _ip_is_metadata(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        packed = socket.inet_aton(host)
        return _ip_is_metadata(ipaddress.ip_address(packed))
    except (OSError, ValueError):
        pass
    return False


def _reject_userinfo_and_scheme(url: str) -> str:
    """Reject embedded userinfo and non-http/https schemes.

    Credentials embedded in the URL (``https://user:pass@host/``) would be
    sent to the target as part of the connection; non-http(s) schemes have
    no legitimate use for AAP controller URLs. Raises ValueError.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise ValueError("Connection URL is invalid") from exc
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("Connection URL must use http or https")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise ValueError("Connection URL must not embed credentials")
    return url


def _resolve_host_bounded(host: str, timeout_secs: float = 5.0) -> Any:
    """Resolve *host* via getaddrinfo on a daemon helper thread (fail-closed).

    Bare ``socket.getaddrinfo`` has no timeout: on the sync request path
    one slow hostname would hang the serving thread indefinitely and
    stall every API route. Raises ValueError on timeout or resolution failure.
    """
    import logging

    try:
        return _run_blocking_bounded(
            lambda: socket.getaddrinfo(host, None, socket.SOCK_STREAM),
            timeout_secs=max(timeout_secs, 1.0),
        )
    except TimeoutError as exc:
        logging.getLogger("aap_migration.utils.ssrf").warning(
            "SSRF strict DNS resolve timed out after %ss for host %s; failing closed",
            max(timeout_secs, 1.0),
            host,
        )
        raise ValueError("Connection URL host resolution timed out; failing closed") from exc
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise ValueError("Connection URL host cannot be resolved") from exc


def _strict_enabled() -> bool:
    # Legacy alias: STRICT=1 forces private blocking (default). Kept so
    # existing deployments keep working; the new escape hatch is
    # AAP_BRIDGE_SSRF_ALLOW_PRIVATE=1.
    return os.environ.get("AAP_BRIDGE_SSRF_STRICT", "").strip().lower() in {"1", "true", "yes"}


def validate_connection_url(url: str, allow_private: bool | None = None) -> str:
    """Reject metadata-service URLs and, by default, private/internal targets.

    Cloud metadata endpoints are always rejected. Resolved private,
    loopback, link-local, multicast, reserved, and unspecified IPs are
    rejected by default (fail closed) because connection URLs become
    API-worker input in Stack 2/5 and must not carry bearer tokens to
    internal targets. Pass ``allow_private=True`` or set
    ``AAP_BRIDGE_SSRF_ALLOW_PRIVATE=1`` for legitimate on-prem RFC1918
    controllers. Userinfo in the URL and non-http/https schemes are always
    rejected. Raises ValueError with a generic message.
    """
    _reject_userinfo_and_scheme(url)
    if is_metadata_url(url):
        raise ValueError("Connection URL targets a blocked metadata endpoint")
    if allow_private is None:
        allow_private = _private_allowed_by_env()
    if not allow_private:
        host = urlparse(url).hostname or ""
        infos = _resolve_host_bounded(host, timeout_secs=5.0)
        for _, _, _, _, sockaddr in infos:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if _is_blocked_private_ip(ip):
                raise ValueError("Connection URL resolves to a blocked private address")
    return url


def reverify_execution_url(url: str, allow_private: bool | None = None) -> str:
    """Re-check a stored connection URL at job-execution time.

    Re-verifies the literal URL and its currently resolved IPs against the
    metadata ranges plus the default private-address rejection (same
    contract as :func:`validate_connection_url`). DNS resolution failures
    fail closed with ValueError. Raises ValueError.
    """
    _reject_userinfo_and_scheme(url)
    if is_metadata_url(url):
        raise ValueError("Connection URL targets a blocked metadata endpoint")
    if allow_private is None:
        allow_private = _private_allowed_by_env()
    try:
        host = urlparse(url).hostname or ""
        if not host:
            raise ValueError("Connection URL host cannot be resolved")
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise ValueError("Connection URL host cannot be resolved") from exc
    for _, _, _, _, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _ip_is_metadata(ip):
            raise ValueError("Connection URL resolves to a blocked metadata endpoint")
        if not allow_private and _is_blocked_private_ip(ip):
            raise ValueError("Connection URL resolves to a blocked private address")
    return url


def reverify_execution_url_bounded(
    url: str,
    timeout_secs: float = 10.0,
    allow_private: bool | None = None,
    use_cache: bool = True,
) -> str:
    """Bounded :func:`reverify_execution_url` for the single FIFO worker.

    Runs the re-verification (including blocking DNS resolution) on a plain
    daemon thread with *timeout_secs* so one poison hostname fails one job
    fast instead of head-of-line blocking every queued job. Successful
    verdicts are cached briefly per host; failures never are. Timeouts fail
    closed with ValueError and emit a warning.

    Pass ``use_cache=False`` for send-path checks that must see a fresh DNS
    verdict (e.g. the post-rate-limit-sleep re-verification in the HTTP
    client, where a rebind during the sleep must not hit a pre-sleep cache
    entry). The fresh result still refreshes the cache for later callers.
    """
    import logging

    if allow_private is None:
        allow_private = _private_allowed_by_env()
    try:
        host = _normalize_host(urlparse(url).hostname or "")
    except Exception:
        host = ""
    cache_key = f"{host}|{allow_private}" if host else ""
    if cache_key and use_cache:
        with _REVERIFY_CACHE_LOCK:
            hit = _REVERIFY_CACHE.get(cache_key)
            if hit and (time.monotonic() - hit[0]) < _REVERIFY_CACHE_TTL_SECS:
                return hit[1]
    try:
        result: str = _run_blocking_bounded(
            lambda: reverify_execution_url(url, allow_private=allow_private),
            timeout_secs=max(timeout_secs, 1.0),
        )
    except TimeoutError as exc:
        logging.getLogger("aap_migration.utils.ssrf").warning(
            "SSRF DNS reverify timed out after %ss for host %s; failing closed",
            max(timeout_secs, 1.0),
            _safe_host_for_log(url),
        )
        raise ValueError("Connection URL host resolution timed out; failing closed") from exc
    if cache_key:
        with _REVERIFY_CACHE_LOCK:
            _REVERIFY_CACHE[cache_key] = (time.monotonic(), result)
            if len(_REVERIFY_CACHE) > 256:
                oldest = min(_REVERIFY_CACHE, key=lambda k: _REVERIFY_CACHE[k][0])
                del _REVERIFY_CACHE[oldest]
    return result
