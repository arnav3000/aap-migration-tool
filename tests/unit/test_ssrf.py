"""Unit tests for SSRF/URL validators (Stack 2/5 review #5)."""

from unittest.mock import patch

import pytest

from aap_migration.utils import ssrf


class TestIsMetadataUrl:
    def test_literal_metadata_ip(self) -> None:
        assert ssrf.is_metadata_url("http://169.254.169.254/") is True
        assert ssrf.is_metadata_url("http://169.254.169.254/latest/meta-data/") is True

    def test_metadata_hostnames(self) -> None:
        assert ssrf.is_metadata_url("http://metadata.google.internal/") is True
        assert ssrf.is_metadata_url("http://metadata.goog/") is True

    def test_extra_metadata_ip(self) -> None:
        assert ssrf.is_metadata_url("http://100.100.100.200/") is True

    def test_octal_hex_spellings_blocked(self) -> None:
        # 169.254.169.254 in octal / hex via inet_aton fallback
        assert ssrf.is_metadata_url("http://0251.0376.0251.0376/") is True
        assert ssrf.is_metadata_url("http://0xa9.0xfe.0xa9.0xfe/") is True

    def test_bracketed_ipv6_link_local(self) -> None:
        assert ssrf.is_metadata_url("http://[fe80::1]/") is True

    def test_ipv4_mapped_ipv6(self) -> None:
        assert ssrf.is_metadata_url("http://[::ffff:169.254.169.254]/") is True

    def test_public_not_metadata(self) -> None:
        assert ssrf.is_metadata_url("http://8.8.8.8/") is False
        assert ssrf.is_metadata_url("https://controller.example.com/api/") is False

    def test_ula_not_metadata(self) -> None:
        # ULA is private space, not a metadata endpoint (#22)
        assert ssrf.is_metadata_url("http://[fd00::1]/") is False

    def test_private_not_metadata_literal(self) -> None:
        assert ssrf.is_metadata_url("http://127.0.0.1/") is False
        assert ssrf.is_metadata_url("http://10.0.0.1/") is False


class TestUserinfoScheme:
    def test_userinfo_rejected(self) -> None:
        with pytest.raises(ValueError):
            ssrf.validate_connection_url("https://user:pass@example.com/", allow_private=True)

    def test_non_http_scheme_rejected(self) -> None:
        with pytest.raises(ValueError):
            ssrf.validate_connection_url("ftp://example.com/", allow_private=True)
        with pytest.raises(ValueError):
            ssrf.validate_connection_url("file:///etc/passwd", allow_private=True)


def _infos(*ips: str) -> list[tuple[None, None, None, None, tuple[str, int]]]:
    return [(None, None, None, None, (ip, 0)) for ip in ips]


class TestValidateDefaultPrivate:
    def test_private_blocked_by_default(self) -> None:
        with patch.object(ssrf.socket, "getaddrinfo", return_value=_infos("10.0.0.5")):
            with pytest.raises(ValueError, match="private"):
                ssrf.validate_connection_url("http://controller.internal/")

    def test_loopback_blocked_by_default(self) -> None:
        with patch.object(ssrf.socket, "getaddrinfo", return_value=_infos("127.0.0.1")):
            with pytest.raises(ValueError, match="private"):
                ssrf.validate_connection_url("http://localhost/")

    def test_public_allowed_by_default(self) -> None:
        with patch.object(ssrf.socket, "getaddrinfo", return_value=_infos("8.8.8.8")):
            assert ssrf.validate_connection_url("http://example.com/") == "http://example.com/"

    def test_allow_private_env_restores_on_prem(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AAP_BRIDGE_SSRF_ALLOW_PRIVATE", "1")
        with patch.object(ssrf.socket, "getaddrinfo", return_value=_infos("10.0.0.5")):
            assert ssrf.validate_connection_url("http://controller.internal/") == (
                "http://controller.internal/"
            )

    def test_metadata_always_blocked_even_with_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AAP_BRIDGE_SSRF_ALLOW_PRIVATE", "1")
        with pytest.raises(ValueError, match="metadata"):
            ssrf.validate_connection_url("http://169.254.169.254/", allow_private=True)


class TestReverifyFailClosed:
    def test_unresolvable_fails_closed(self) -> None:
        import socket as sock

        with patch.object(ssrf.socket, "getaddrinfo", side_effect=sock.gaierror("nope")):
            with pytest.raises(ValueError, match="cannot be resolved"):
                ssrf.reverify_execution_url("http://unresolvable.invalid/")

    def test_timeout_fails_closed(self) -> None:
        with patch("aap_migration.utils.ssrf._run_blocking_bounded", side_effect=TimeoutError):
            with pytest.raises(ValueError, match="timed out"):
                ssrf.reverify_execution_url_bounded("http://example.com/", timeout_secs=1)

    def test_success_cached(self) -> None:
        with patch.object(ssrf, "reverify_execution_url", return_value="http://example.com/") as m:
            ssrf._REVERIFY_CACHE.clear()
            ssrf.reverify_execution_url_bounded("http://example.com/", timeout_secs=5)
            ssrf.reverify_execution_url_bounded("http://example.com/", timeout_secs=5)
            assert m.call_count == 1
            ssrf._REVERIFY_CACHE.clear()

    def test_send_path_bypasses_cache(self) -> None:
        with patch.object(ssrf, "reverify_execution_url", return_value="http://example.com/") as m:
            ssrf._REVERIFY_CACHE.clear()
            ssrf.reverify_execution_url_bounded("http://example.com/", timeout_secs=5)
            assert m.call_count == 1
            # Post-sleep send-path check must see fresh DNS, not the cache
            ssrf.reverify_execution_url_bounded(
                "http://example.com/", timeout_secs=5, use_cache=False
            )
            assert m.call_count == 2
            ssrf._REVERIFY_CACHE.clear()


class TestReverifyResolvedMetadata:
    """Pin the resolved-IP metadata branch of reverify_execution_url (#8)."""

    def test_resolved_metadata_ip_blocked(self) -> None:
        with patch.object(ssrf.socket, "getaddrinfo", return_value=_infos("169.254.169.254")):
            with pytest.raises(ValueError, match="metadata"):
                ssrf.reverify_execution_url("http://example.com/")

    def test_resolved_alibaba_metadata_blocked(self) -> None:
        with patch.object(ssrf.socket, "getaddrinfo", return_value=_infos("100.100.100.200")):
            with pytest.raises(ValueError, match="metadata"):
                ssrf.reverify_execution_url("http://example.com/")

    def test_resolved_private_ip_blocked(self) -> None:
        with patch.object(ssrf.socket, "getaddrinfo", return_value=_infos("127.0.0.1")):
            with pytest.raises(ValueError, match="private"):
                ssrf.reverify_execution_url("http://example.com/")

    def test_bounded_variant_blocks_metadata_fresh(self) -> None:
        ssrf._REVERIFY_CACHE.clear()
        try:
            with patch.object(ssrf.socket, "getaddrinfo", return_value=_infos("169.254.169.254")):
                with pytest.raises(ValueError, match="metadata"):
                    ssrf.reverify_execution_url_bounded(
                        "http://example.com/", timeout_secs=5, use_cache=False
                    )
        finally:
            ssrf._REVERIFY_CACHE.clear()


class TestDnsBound:
    def test_semaphore_refusal_fails_closed(self) -> None:
        held = 0
        try:
            while ssrf._DNS_SEMAPHORE.acquire(blocking=False):
                held += 1
            with pytest.raises(TimeoutError, match="too many concurrent"):
                ssrf._run_blocking_bounded(lambda: "never", timeout_secs=5)
        finally:
            for _ in range(held):
                ssrf._DNS_SEMAPHORE.release()
