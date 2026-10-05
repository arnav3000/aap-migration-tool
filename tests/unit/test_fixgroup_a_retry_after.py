"""Fixgroup A: defensive Retry-After parsing (pre-existing base_client:240).

Non-integer (HTTP-date per RFC 7231) Retry-After headers must not crash
``int()``: int in try/except, HTTP-date fallback, else default backoff
(``retry_after=None``).
"""

from __future__ import annotations

import time
from email.utils import formatdate

import httpx
import pytest

from aap_migration.client.base_client import BaseAPIClient, _parse_retry_after
from aap_migration.client.exceptions import RateLimitError


def test_int_seconds() -> None:
    assert _parse_retry_after("5") == 5
    assert _parse_retry_after("0") == 0


def test_missing_or_empty() -> None:
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None


def test_http_date_future() -> None:
    future = formatdate(time.time() + 120, usegmt=True)
    parsed = _parse_retry_after(future)
    assert parsed is not None and 0 < parsed <= 120


def test_http_date_past_clamps_to_zero() -> None:
    past = formatdate(time.time() - 60, usegmt=True)
    assert _parse_retry_after(past) == 0


def test_garbage_falls_back_to_none() -> None:
    assert _parse_retry_after("not-a-date") is None
    assert _parse_retry_after("5.5") is None


def test_negative_and_padded_clamp_to_non_negative() -> None:
    assert _parse_retry_after("-5") == 0
    assert _parse_retry_after(" 5 ") == 5


def _client() -> BaseAPIClient:
    return BaseAPIClient(base_url="https://aap.example.com", token="t")


def test_handler_429_http_date_does_not_crash() -> None:
    import asyncio

    client = _client()
    try:
        future = formatdate(time.time() + 60, usegmt=True)
        resp = httpx.Response(429, headers={"Retry-After": future}, json={"detail": "slow"})
        with pytest.raises(RateLimitError) as excinfo:
            client._handle_error_response(resp)
        assert excinfo.value.retry_after is not None
        assert 0 <= excinfo.value.retry_after <= 60
    finally:
        asyncio.run(client.close())


def test_handler_429_garbage_header_defaults() -> None:
    import asyncio

    client = _client()
    try:
        resp = httpx.Response(429, headers={"Retry-After": "soon-ish"}, json={"detail": "slow"})
        with pytest.raises(RateLimitError) as excinfo:
            client._handle_error_response(resp)
        assert excinfo.value.retry_after is None
    finally:
        asyncio.run(client.close())
