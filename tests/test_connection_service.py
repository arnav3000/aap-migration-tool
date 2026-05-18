from __future__ import annotations

from dataclasses import dataclass

import pytest

from aap_migration.api import crypto
from aap_migration.api.models import Connection
from aap_migration.api.services.connection_service import ConnectionService


def test_create_and_update_encrypt_tokens(db_session) -> None:
    conn = ConnectionService.create(
        db_session,
        name="Source",
        url="https://source.example.com",
        token="plain-token",
        role="source",
    )
    db_session.commit()

    assert conn.token != "plain-token"
    assert crypto.decrypt_token(conn.token) == "plain-token"

    updated = ConnectionService.update(db_session, conn.id, token="new-token", timeout=90)
    db_session.commit()

    assert updated is not None
    assert crypto.decrypt_token(updated.token) == "new-token"
    assert updated.timeout == 90


def test_build_instance_config_decrypts_legacy_plaintext_token(db_session) -> None:
    conn = Connection(
        name="Legacy",
        url="https://target.example.com",
        token="legacy-token",
        type="aap",
        role="target",
        verify_ssl=False,
        timeout=60,
    )
    db_session.add(conn)
    db_session.commit()

    config = ConnectionService.build_instance_config(conn)

    assert config.url == "https://target.example.com"
    assert config.token == "legacy-token"
    assert config.verify_ssl is False
    assert config.timeout == 60


def test_auth_scheme_distinguishes_awx_from_aap(db_session) -> None:
    awx = Connection(
        name="AWX", url="https://awx.example.com", token="t", type="awx", role="source"
    )
    aap = Connection(
        name="AAP", url="https://aap.example.com", token="t", type="aap", role="target"
    )

    assert ConnectionService._auth_scheme(awx) == "Token"
    assert ConnectionService._auth_scheme(aap) == "Bearer"


@pytest.mark.asyncio
async def test_test_connection_uses_source_client_for_source_role(
    monkeypatch: pytest.MonkeyPatch, db_session
) -> None:
    calls: list[tuple[str, str, str]] = []

    @dataclass
    class FakeClient:
        config: object
        auth_scheme: str
        label: str

        async def __aenter__(self):
            calls.append((self.label, self.auth_scheme, "enter"))
            return self

        async def __aexit__(self, exc_type, exc, tb):
            calls.append((self.label, self.auth_scheme, "exit"))

        async def get(self, path: str):
            calls.append((self.label, self.auth_scheme, path))
            return {"ok": True}

    monkeypatch.setattr(
        "aap_migration.api.services.connection_service.AAPSourceClient",
        lambda config, auth_scheme: FakeClient(config, auth_scheme, "source"),
    )
    monkeypatch.setattr(
        "aap_migration.api.services.connection_service.AAPTargetClient",
        lambda config, auth_scheme: FakeClient(config, auth_scheme, "target"),
    )

    conn = ConnectionService.create(
        db_session,
        name="Source",
        url="https://source.example.com",
        token="source-token",
        type="awx",
        role="source",
    )
    db_session.commit()

    ok, error = await ConnectionService.test_connection(conn)

    assert (ok, error) == (True, None)
    assert calls == [
        ("source", "Token", "enter"),
        ("source", "Token", "me/"),
        ("source", "Token", "exit"),
    ]


@pytest.mark.asyncio
async def test_test_connection_uses_target_client_and_returns_error(
    monkeypatch: pytest.MonkeyPatch, db_session
) -> None:
    class FailingClient:
        def __init__(self, config: object, auth_scheme: str) -> None:
            self.auth_scheme = auth_scheme

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, path: str):
            raise RuntimeError(f"{self.auth_scheme}:{path}:boom")

    monkeypatch.setattr(
        "aap_migration.api.services.connection_service.AAPSourceClient",
        FailingClient,
    )
    monkeypatch.setattr(
        "aap_migration.api.services.connection_service.AAPTargetClient",
        FailingClient,
    )

    conn = ConnectionService.create(
        db_session,
        name="Target",
        url="https://target.example.com",
        token="target-token",
        type="aap",
        role="target",
    )
    db_session.commit()

    ok, error = await ConnectionService.test_connection(conn)

    assert ok is False
    assert error == "Bearer:me/:boom"
