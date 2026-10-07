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


def test_single_stale_key_contained_without_locking_ip(monkeypatch: Any) -> None:
    """P1 #10: one looping stale key 429s against itself, not the fleet.

    Ten rapid failures on one stale key burn out against the per-key
    bucket (401 x5, then key-scoped 429s that do not feed the shared IP
    bucket), so distinct keys from the same egress IP still get plain
    401s instead of a fleet-wide 429.
    """
    monkeypatch.setenv("AAP_BRIDGE_API_TOKEN", "correct-token-1234567890")
    monkeypatch.delenv("AAP_BRIDGE_API_TOKEN_SECONDARY", raising=False)
    sec._auth_failures.clear()
    sec._auth_key_failures.clear()
    try:
        codes = []
        for _ in range(10):
            try:
                sec.require_api_key("stale-token", _req("10.9.9.9"))
            except HTTPException as exc:
                codes.append(exc.status_code)
        assert codes[:5] == [401] * 5, codes
        assert codes[5:] == [429] * 5, codes
        # Same egress, different keys: the shared IP budget only saw the
        # first five attempts, so these stay 401 (no fleet lockout).
        for i in range(10):
            try:
                sec.require_api_key(f"other-{i}", _req("10.9.9.9"))
            except HTTPException as exc:
                assert exc.status_code == 401, f"attempt {i}: {exc.status_code}"
            else:
                raise AssertionError(f"attempt {i} unexpectedly authenticated")
        # Rotating-key guessing still trips the shared IP 429 (backstop).
        last_status: int | None = None
        for i in range(10):
            try:
                sec.require_api_key(f"fresh-{i}", _req("10.9.9.9"))
            except HTTPException as exc:
                last_status = exc.status_code
                assert exc.status_code in (401, 429)
            else:
                raise AssertionError("unexpectedly authenticated")
        assert last_status == 429
    finally:
        sec._auth_failures.clear()
        sec._auth_key_failures.clear()
