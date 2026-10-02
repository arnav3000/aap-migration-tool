"""Fixgroup A: redirect re-verification stays off the event loop (P1 #5).

The redirect-loop ``reverify_execution_url_bounded(target)`` call must go
through ``await asyncio.to_thread(...)`` like the pre-request check, so a
slow redirect-target lookup cannot stall concurrent API traffic.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


async def test_redirect_reverify_offloaded_to_thread(monkeypatch: Any) -> None:
    import aap_migration.utils.ssrf as ssrf
    from aap_migration.client.base_client import BaseAPIClient

    monkeypatch.setattr(ssrf, "reverify_execution_url_bounded", lambda url: url)
    reverify = ssrf.reverify_execution_url_bounded

    calls: list[tuple[Any, tuple[Any, ...]]] = []

    async def _spy_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        calls.append((func, args))
        assert callable(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _spy_to_thread)

    client = BaseAPIClient(base_url="https://aap.example.com", token="t")
    try:
        responses = [
            httpx.Response(302, headers={"location": "/next"}),
            httpx.Response(200, json={"ok": True}),
        ]

        async def _fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
            return responses.pop(0)

        monkeypatch.setattr(client.client, "request", _fake_request)
        out = await client.request("GET", "start")
        assert out == {"ok": True}
    finally:
        await client.close()

    targets = [args[0] for func, args in calls if func is reverify]
    # Pre-request check (base URL) plus the redirect target both offloaded.
    assert len(targets) >= 2, targets
    assert any(str(t).endswith("/next") for t in targets), targets
