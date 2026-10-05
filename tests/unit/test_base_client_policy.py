"""Unit tests for BaseAPIClient redirect + Retry-After policy (review #10/#11)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aap_migration.client.base_client import _parse_retry_after


class TestParseRetryAfter:
    def test_integer(self) -> None:
        assert _parse_retry_after("120") == 120

    def test_missing_garbage_none(self) -> None:
        assert _parse_retry_after(None) is None
        assert _parse_retry_after("") is None
        assert _parse_retry_after("garbage") is None

    def test_http_date_capped(self) -> None:
        # Far-future date must not park the FIFO worker (#12)
        assert _parse_retry_after("Wed, 01 Jan 2040 00:00:00 GMT") == 300

    def test_negative_clamped(self) -> None:
        assert _parse_retry_after("-5") == 0

    def test_integer_capped_at_max(self) -> None:
        assert _parse_retry_after("1000") == 300

    def test_float_shape_returns_none(self) -> None:
        assert _parse_retry_after("12.5") is None

    def test_past_date_returns_zero(self) -> None:
        assert _parse_retry_after("Wed, 01 Jan 2020 00:00:00 GMT") == 0

    def test_naive_date_assumed_utc_capped(self) -> None:
        assert _parse_retry_after("Mon, 01 Jan 2040 00:00:00") == 300


def _resp(status: int, location: str | None = None, text: str = "{}") -> httpx.Response:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.headers = {"location": location} if location else {}
    r.text = text
    r.json.return_value = {}
    return r


def _client_with_responses(responses: list[httpx.Response]) -> Any:
    from aap_migration.client.base_client import BaseAPIClient

    with patch("aap_migration.utils.ssrf.reverify_execution_url_bounded", return_value="ok"):
        client = BaseAPIClient(base_url="https://ctrl.example.com", token="t")
    client.client.request = AsyncMock(side_effect=responses)
    return client


class TestRedirectPolicy:
    async def test_same_origin_redirect_preserves_params(self) -> None:
        from aap_migration.client import base_client as bc

        first = _resp(301, "/api/v2/jobs/?page=2")
        # Same-origin absolute target after urljoin; params must be forwarded
        second = _resp(200)
        client = _client_with_responses([first, second])
        with patch.object(bc, "reverify_execution_url_bounded", return_value="ok"):
            await client.request("GET", "api/v2/jobs/", params={"page": 1})
        assert client.client.request.call_count == 2
        follow_kwargs = client.client.request.call_args_list[1].kwargs
        assert follow_kwargs["params"] == {"page": 1}

    async def test_cross_origin_redirect_blocked(self) -> None:
        from aap_migration.client import base_client as bc

        first = _resp(302, "https://evil.example.com/steal")
        client = _client_with_responses([first])
        with patch.object(bc, "reverify_execution_url_bounded", return_value="ok"):
            with pytest.raises(Exception, match="Cross-origin"):
                await client.request("GET", "api/v2/jobs/")
        # Bearer token sent only once (no follow-up to evil host)
        assert client.client.request.call_count == 1

    async def test_303_rewritten_to_get_without_body(self) -> None:
        from aap_migration.client import base_client as bc

        first = _resp(303, "/api/v2/next/")
        second = _resp(200)
        client = _client_with_responses([first, second])
        with patch.object(bc, "reverify_execution_url_bounded", return_value="ok"):
            await client.request("POST", "api/v2/jobs/", json_data={"a": 1})
        follow_kwargs = client.client.request.call_args_list[1].kwargs
        assert follow_kwargs["method"] == "GET"
        # Body stripped on 303
        assert follow_kwargs.get("json") is None

    async def test_307_post_preserves_method_and_body(self) -> None:
        from aap_migration.client import base_client as bc

        first = _resp(307, "/api/v2/next/")
        second = _resp(200)
        client = _client_with_responses([first, second])
        with patch.object(bc, "reverify_execution_url_bounded", return_value="ok"):
            await client.request("POST", "api/v2/jobs/", json_data={"a": 1})
        follow_kwargs = client.client.request.call_args_list[1].kwargs
        assert follow_kwargs["method"] == "POST"
        assert follow_kwargs.get("json") == {"a": 1}

    async def test_301_post_rewritten_to_get_without_body(self) -> None:
        from aap_migration.client import base_client as bc

        first = _resp(301, "/api/v2/next/")
        second = _resp(200)
        client = _client_with_responses([first, second])
        with patch.object(bc, "reverify_execution_url_bounded", return_value="ok"):
            await client.request("POST", "api/v2/jobs/", json_data={"a": 1})
        follow_kwargs = client.client.request.call_args_list[1].kwargs
        assert follow_kwargs["method"] == "GET"
        assert follow_kwargs.get("json") is None

    async def test_cyclic_redirect_blocked(self) -> None:
        from aap_migration.client import base_client as bc

        first = _resp(301, "/hop/")
        back = _resp(301, "https://ctrl.example.com/api/v2/jobs/")
        client = _client_with_responses([first, back])
        with patch.object(bc, "reverify_execution_url_bounded", return_value="ok"):
            with pytest.raises(Exception, match="Cyclic redirect"):
                await client.request("GET", "api/v2/jobs/")
        assert client.client.request.call_count == 2

    async def test_non_http_redirect_scheme_blocked(self) -> None:
        from aap_migration.client import base_client as bc

        first = _resp(302, "ftp://ctrl.example.com/file")
        client = _client_with_responses([first])
        with patch.object(bc, "reverify_execution_url_bounded", return_value="ok"):
            with pytest.raises(Exception, match="unsupported scheme"):
                await client.request("GET", "api/v2/jobs/")
        assert client.client.request.call_count == 1

    async def test_absolute_same_origin_redirect_followed(self) -> None:
        from aap_migration.client import base_client as bc

        first = _resp(302, "https://ctrl.example.com/api/v2/next/?page=2")
        second = _resp(200)
        client = _client_with_responses([first, second])
        with patch.object(bc, "reverify_execution_url_bounded", return_value="ok"):
            await client.request("GET", "api/v2/jobs/", params={"page": 1})
        assert client.client.request.call_count == 2
        follow_kwargs = client.client.request.call_args_list[1].kwargs
        assert follow_kwargs["params"] == {"page": 1}

    async def test_terminal_redirect_raises(self) -> None:
        from aap_migration.client import base_client as bc

        chain = [_resp(301, f"/hop{i}/") for i in range(4)] + [_resp(200)]
        client = _client_with_responses(chain)
        with patch.object(bc, "reverify_execution_url_bounded", return_value="ok"):
            with pytest.raises(Exception, match="Redirect chain"):
                await client.request("GET", "api/v2/jobs/")

    async def test_follow_redirects_kwarg_ignored(self) -> None:
        from aap_migration.client import base_client as bc

        ok = _resp(200)
        client = _client_with_responses([ok])
        with patch.object(bc, "reverify_execution_url_bounded", return_value="ok"):
            await client.request("GET", "api/v2/jobs/", follow_redirects=True)
        _, kwargs = (
            client.client.request.call_args_list[0][:2]
            if len(client.client.request.call_args_list[0]) > 2
            else (None, client.client.request.call_args.kwargs)
        )
        assert "follow_redirects" not in client.client.request.call_args.kwargs

    async def test_pre_request_blocked_no_requests(self) -> None:
        from aap_migration.client import base_client as bc
        from aap_migration.client.exceptions import NetworkError

        ok = _resp(200)
        client = _client_with_responses([ok])
        with patch.object(
            bc, "reverify_execution_url_bounded", side_effect=ValueError("blocked url")
        ):
            with pytest.raises(NetworkError, match="SSRF re-verification blocked request"):
                await client.request("GET", "api/v2/jobs/")
        assert client.client.request.call_count == 0

    async def test_pre_request_check_bypasses_cache(self) -> None:
        # Single post-rate-limit check must see fresh DNS, not a cached verdict
        from aap_migration.client import base_client as bc

        ok = _resp(200)
        client = _client_with_responses([ok])
        seen: dict[str, Any] = {}

        def _fake(url: str, **kwargs: Any) -> str:
            seen["url"] = url
            seen.update(kwargs)
            return "ok"

        with patch.object(bc, "reverify_execution_url_bounded", side_effect=_fake):
            await client.request("GET", "api/v2/jobs/")
        assert seen.get("use_cache") is False
        assert seen["url"].endswith("api/v2/jobs/")

    async def test_redirect_target_blocked_no_followup(self) -> None:
        from aap_migration.client import base_client as bc
        from aap_migration.client.exceptions import NetworkError

        first = _resp(302, "/api/v2/next/")
        client = _client_with_responses([first])
        with patch.object(
            bc,
            "reverify_execution_url_bounded",
            side_effect=["ok", ValueError("blocked target")],
        ):
            with pytest.raises(NetworkError, match="Redirect target blocked"):
                await client.request("GET", "api/v2/jobs/")
        # Initial 302 consumed, no follow-up request to the blocked target
        assert client.client.request.call_count == 1
