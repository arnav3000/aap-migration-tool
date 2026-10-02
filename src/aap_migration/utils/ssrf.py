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
from typing import Any
from urllib.parse import urlparse

_SHARED_DNS_POOL: Any | None = None
_SHARED_DNS_POOL_LOCK = threading.Lock()

_METADATA_HOSTNAMES = {"metadata.google.internal", "metadata.goog"}
_METADATA_V4 = ipaddress.ip_network("169.254.0.0/16")
_METADATA_V4_EXTRA = {"100.100.100.200"}
_METADATA_V6 = (
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00::/8"),
)


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


def is_metadata_url(url: str) -> bool:
    """Return True when *url* targets cloud instance-metadata endpoints.

    Matches literal hostnames, IPv4/IPv6 literals (including bracketed and
    IPv4-mapped IPv6 forms), decimal/octal/hex IPv4 spellings, and the
    metadata ranges 169.254.0.0/16, fe80::/10, fd00::/8, plus 100.100.100.200.
    DNS names are NOT resolved here (no network I/O at validation import
    time); use strict mode in validate_connection_url for resolved-IP checks.
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


def validate_connection_url(url: str) -> str:
    """Reject metadata-service URLs; private AAP hosts stay allowed.

    Private AAP controllers legitimately live on RFC1918 networks, so private
    IPs are allowed by default. Set ``AAP_BRIDGE_SSRF_STRICT=1`` to also
    reject private/loopback/link-local hosts. Cloud metadata endpoints are
    always rejected. Userinfo (username/password) in the URL and
    non-http/https schemes are always rejected. Raises ValueError with a
    generic message (no backend detail echoed).
    """
    _reject_userinfo_and_scheme(url)
    if is_metadata_url(url):
        raise ValueError("Connection URL targets a blocked metadata endpoint")
    if os.environ.get("AAP_BRIDGE_SSRF_STRICT", "").strip() in {"1", "true", "yes"}:
        try:
            host = urlparse(url).hostname or ""
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError) as exc:
            raise ValueError("Connection URL host cannot be resolved") from exc
        for _, _, _, _, sockaddr in infos:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
                raise ValueError("Connection URL resolves to a blocked private address")
    return url


def reverify_execution_url(url: str) -> str:
    """Re-check a stored connection URL at job-execution time.

    Create/update validation runs once, but a benign hostname can be rebound
    (or the stored URL edited) to a metadata endpoint before the worker
    fetches: the job's ping/version calls would then deliver the bearer
    token to the metadata service. This re-verifies the literal URL and its
    currently resolved IPs against the metadata ranges. Private AAP hosts
    stay allowed (strict private blocking remains opt-in via
    ``AAP_BRIDGE_SSRF_STRICT``). DNS resolution failures fail closed with
    ValueError (fail-safe: a host that cannot be verified must not receive
    the bearer token). Raises ValueError.
    """
    _reject_userinfo_and_scheme(url)
    if is_metadata_url(url):
        raise ValueError("Connection URL targets a blocked metadata endpoint")
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
    return url


def reverify_execution_url_bounded(url: str, timeout_secs: float = 10.0) -> str:
    """Bounded :func:`reverify_execution_url` for the single FIFO worker.

    Runs the re-verification (including blocking DNS resolution) on a
    dedicated single-worker daemon-thread executor per call with
    *timeout_secs* so one poison hostname fails one job fast instead of
    head-of-line blocking every queued job. Per-call isolation (not a
    shared singleton pool) prevents hung ``getaddrinfo`` tasks from
    exhausting 4 shared slots and failing healthy jobs closed: a hung
    lookup occupies only its own daemon thread, which is abandoned on
    timeout and exits on its own, while the next probe gets a fresh
    thread. Timeouts fail closed with ValueError and emit a warning so
    pool saturation is observable.
    """
    import concurrent.futures
    import logging

    pool = _daemon_dns_pool(max_workers=1)
    future = pool.submit(reverify_execution_url, url)
    try:
        result: str = future.result(timeout=max(timeout_secs, 1.0))
        return result
    except concurrent.futures.TimeoutError as exc:
        # Do not block the FIFO worker on pool shutdown: the hung DNS
        # task stays on its daemon worker (which exits on its own) while
        # the caller fails fast. Per-call isolation means the hung slot
        # is never reused, so healthy probes are unaffected.
        logging.getLogger("aap_migration.utils.ssrf").warning(
            "SSRF DNS reverify timed out after %ss for host %s; "
            "abandoning hung lookup thread (per-call isolation, "
            "healthy probes unaffected)",
            max(timeout_secs, 1.0),
            _safe_host_for_log(url),
        )
        raise ValueError("Connection URL host resolution timed out; failing closed") from exc
    finally:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


def _daemon_dns_pool(max_workers: int = 4) -> Any:
    """Create a bounded daemon-thread pool for DNS re-verification."""
    import concurrent.futures
    import threading

    pool: Any = concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="ssrf-dns"
    )
    _orig_adjust = pool._adjust_thread_count

    def _daemon_adjust() -> None:
        _orig_thread = threading.Thread

        class _DaemonThread(_orig_thread):  # type: ignore[valid-type,misc]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["daemon"] = True
                super().__init__(*args, **kwargs)

        threading.Thread = _DaemonThread  # type: ignore[misc]
        try:
            _orig_adjust()
        finally:
            threading.Thread = _orig_thread  # type: ignore[misc]

    pool._adjust_thread_count = _daemon_adjust
    return pool


def _shared_dns_pool() -> Any:
    """Singleton shared bounded daemon DNS executor (lazy, thread-safe)."""
    global _SHARED_DNS_POOL
    if _SHARED_DNS_POOL is not None:
        return _SHARED_DNS_POOL
    with _SHARED_DNS_POOL_LOCK:
        if _SHARED_DNS_POOL is None:
            _SHARED_DNS_POOL = _daemon_dns_pool(max_workers=4)
        return _SHARED_DNS_POOL
