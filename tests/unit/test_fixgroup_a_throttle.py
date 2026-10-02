"""Fixgroup A: auth throttle keys on IP alone (P1 #4).

Rotating X-API-Key per guess must not dodge the 20/min 429: >20 distinct
bad keys from one IP must yield 429.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException

from aap_migration.api import security as sec


def _req(ip: str) -> Any:
    return SimpleNamespace(client=SimpleNamespace(host=ip))


def test_rotating_keys_from_one_ip_trips_429(monkeypatch: Any) -> None:
    monkeypatch.setenv("AAP_BRIDGE_API_TOKEN", "correct-token-1234567890")
    monkeypatch.delenv("AAP_BRIDGE_API_TOKEN_SECONDARY", raising=False)
    sec._auth_failures.clear()
    try:
        last_status: int | None = None
        for i in range(25):
            try:
                sec.require_api_key(f"guess-{i}", _req("10.9.9.9"))
            except HTTPException as exc:
                last_status = exc.status_code
                if i < 20:
                    assert exc.status_code == 401, f"attempt {i}: {exc.status_code}"
            else:
                raise AssertionError(f"attempt {i} unexpectedly authenticated")
        assert last_status == 429
    finally:
        sec._auth_failures.clear()


def test_success_resets_ip_bucket(monkeypatch: Any) -> None:
    monkeypatch.setenv("AAP_BRIDGE_API_TOKEN", "correct-token-1234567890")
    monkeypatch.delenv("AAP_BRIDGE_API_TOKEN_SECONDARY", raising=False)
    sec._auth_failures.clear()
    try:
        for i in range(5):
            try:
                sec.require_api_key(f"bad-{i}", _req("10.9.9.8"))
            except HTTPException as exc:
                assert exc.status_code == 401
        # Legit success clears the IP bucket ...
        sec.require_api_key("correct-token-1234567890", _req("10.9.9.8"))
        # ... so the next bad guess starts counting from 1 (401, not 429).
        try:
            sec.require_api_key("bad-again", _req("10.9.9.8"))
        except HTTPException as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("expected 401")
    finally:
        sec._auth_failures.clear()
