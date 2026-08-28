from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from aap_migration.client.base_client import BaseAPIClient
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


def build_response(
    status_code: int,
    *,
    json_data=None,
    text: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    request = httpx.Request("GET", "https://example.com/api/v2/test")
    if json_data is not None:
        return httpx.Response(status_code, request=request, json=json_data, headers=headers)
    return httpx.Response(status_code, request=request, text=text or "", headers=headers)


def test_base_client_builds_headers_and_urls() -> None:
    client = BaseAPIClient("https://example.com/api/v2/", "token-1", auth_scheme="Token")
    try:
        assert client._build_headers()["Authorization"] == "Token token-1"
        assert client._build_url("/jobs/") == "https://example.com/api/v2/jobs/"
    finally:
        asyncio.run(client.close())


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (build_response(401, json_data={"detail": "auth"}), AuthenticationError),
        (build_response(403, json_data={"detail": "forbidden"}), AuthorizationError),
        (build_response(404, json_data={"detail": "missing"}), NotFoundError),
        (build_response(500, json_data={"detail": "boom"}), ServerError),
        (build_response(418, json_data={"detail": "teapot"}), APIError),
    ],
)
def test_handle_error_response_maps_status_codes(
    response: httpx.Response, error_type: type[Exception]
) -> None:
    client = BaseAPIClient("https://example.com/api/v2", "token-1")
    try:
        with pytest.raises(error_type):
            client._handle_error_response(response)
    finally:
        import asyncio

        asyncio.run(client.close())


def test_handle_error_response_maps_conflicts_and_rate_limits() -> None:
    client = BaseAPIClient("https://example.com/api/v2", "token-1")
    try:
        with pytest.raises(PendingDeletionError):
            client._handle_error_response(
                build_response(409, json_data={"detail": "already pending deletion"})
            )

        with pytest.raises(ResourceInUseError) as in_use:
            client._handle_error_response(
                build_response(
                    409,
                    json_data={
                        "detail": "resource is being used by running jobs",
                        "active_jobs": [{"id": 7}],
                    },
                )
            )
        assert in_use.value.active_jobs == [{"id": 7}]

        with pytest.raises(ConflictError, match="may already exist"):
            client._handle_error_response(
                build_response(409, json_data={"detail": "already exists"})
            )

        with pytest.raises(RateLimitError) as limited:
            client._handle_error_response(
                build_response(429, json_data={"detail": "slow down"}, headers={"Retry-After": "9"})
            )
        assert limited.value.retry_after == 9

        with pytest.raises(APIError, match="x, y"):
            client._handle_error_response(build_response(400, json_data=["x", "y"]))
    finally:
        import asyncio

        asyncio.run(client.close())


@pytest.mark.asyncio
async def test_request_and_http_method_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BaseAPIClient("https://example.com/api/v2", "token-1")
    calls: list[dict[str, object]] = []

    async def fake_request(**kwargs):
        calls.append(kwargs)
        return build_response(
            200,
            json_data={"ok": True},
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(client.client, "request", fake_request)

    try:
        assert await client.request("POST", "/jobs/", json_data={"name": "demo"}) == {"ok": True}
        assert await client.get("jobs/") == {"ok": True}
        assert await client.post("jobs/", json_data={"a": 1}) == {"ok": True}
        assert await client.put("jobs/1/", json_data={"a": 2}) == {"ok": True}
        assert await client.patch("jobs/1/", json_data={"a": 3}) == {"ok": True}
        assert await client.delete("jobs/1/") == {"ok": True}
        assert await client.options("jobs/", suppress_server_error=True) == {"ok": True}
    finally:
        await client.close()

    assert calls[0]["method"] == "POST"
    assert calls[0]["json"] == {"name": "demo"}
    assert "suppress_server_error" not in calls[-1]


@pytest.mark.asyncio
async def test_request_handles_empty_invalid_and_non_json_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = BaseAPIClient("https://example.com/api/v2", "token-1")
    responses = iter(
        [
            build_response(204, text=""),
            build_response(200, text="not-json", headers={"content-type": "text/plain"}),
            build_response(200, text="not-json", headers={"content-type": "application/json"}),
        ]
    )

    async def fake_request(**kwargs):
        return next(responses)

    monkeypatch.setattr(client.client, "request", fake_request)

    try:
        assert await client.request("GET", "empty") == {}
        with pytest.raises(NetworkError, match="Non-JSON response"):
            await client.request("GET", "plain-text")
        with pytest.raises(json.JSONDecodeError):
            await client.request("GET", "bad-json")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_request_wraps_network_timeout_and_context_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = BaseAPIClient("https://example.com/api/v2", "token-1")
    closed = {"value": False}

    async def raise_network(**kwargs):
        raise httpx.NetworkError("offline")

    monkeypatch.setattr(client.client, "request", raise_network)

    with pytest.raises(NetworkError, match="offline"):
        await client.request("GET", "jobs/")

    async def raise_timeout(**kwargs):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(client.client, "request", raise_timeout)

    with pytest.raises(NetworkError, match="Request timeout"):
        await client.request("GET", "jobs/")

    async def fake_aclose() -> None:
        closed["value"] = True

    monkeypatch.setattr(client.client, "aclose", fake_aclose)

    async with client as entered:
        assert entered is client

    assert closed["value"] is True
