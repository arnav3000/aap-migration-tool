from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from aap_migration.api import crypto, dependencies
from aap_migration.api.app import (
    _migrate_add_seq_id,
    _recover_stale_jobs,
    _seed_connections_from_env,
    create_app,
)
from aap_migration.api.models import Connection, JobRecord
from aap_migration.migration.database import create_database_engine


def test_encrypt_and_decrypt_round_trip() -> None:
    encrypted = crypto.encrypt_token("super-secret")

    assert encrypted.startswith("gAAAAA")
    assert crypto.decrypt_token(encrypted) == "super-secret"


def test_decrypt_legacy_plaintext_token_returns_original_value() -> None:
    assert crypto.decrypt_token("legacy-token") == "legacy-token"
    assert crypto.decrypt_token("") == ""


def test_missing_encryption_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AAP_TOKEN_ENCRYPTION_KEY", raising=False)
    crypto._fernet = None

    with pytest.raises(RuntimeError, match="AAP_TOKEN_ENCRYPTION_KEY must be set"):
        crypto.ensure_encryption_key_configured()


def test_decrypt_with_wrong_key_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AAP_TOKEN_ENCRYPTION_KEY", "first-key")
    crypto._fernet = None
    encrypted = crypto.encrypt_token("secret")

    monkeypatch.setenv("AAP_TOKEN_ENCRYPTION_KEY", "second-key")
    crypto._fernet = None

    with pytest.raises(ValueError, match="Stored token cannot be decrypted"):
        crypto.decrypt_token(encrypted)


def test_get_db_url_uses_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIGRATION_STATE_DB_PATH", "sqlite:///override.db")

    assert dependencies.get_db_url() == "sqlite:///override.db"


def test_get_db_url_defaults_to_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIGRATION_STATE_DB_PATH", raising=False)

    assert dependencies.get_db_url().startswith("postgresql://")


def test_get_app_state_raises_before_initialization() -> None:
    dependencies._app_state = None

    with pytest.raises(RuntimeError, match="AppState not initialized"):
        dependencies.get_app_state()


def test_get_db_commits_and_closes_session() -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.committed = False
            self.rolled_back = False
            self.closed = False

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    session = FakeSession()
    dependencies.set_app_state(
        dependencies.AppState(db_session_factory=lambda: session, job_service=SimpleNamespace())
    )

    gen = dependencies.get_db()
    yielded = next(gen)

    assert yielded is session
    with pytest.raises(StopIteration):
        next(gen)
    assert session.committed is True
    assert session.rolled_back is False
    assert session.closed is True


def test_get_db_rolls_back_on_error() -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.committed = False
            self.rolled_back = False
            self.closed = False

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    session = FakeSession()
    dependencies.set_app_state(
        dependencies.AppState(db_session_factory=lambda: session, job_service=SimpleNamespace())
    )

    gen = dependencies.get_db()
    next(gen)

    with pytest.raises(RuntimeError, match="boom"):
        gen.throw(RuntimeError("boom"))
    assert session.committed is False
    assert session.rolled_back is True
    assert session.closed is True


def test_create_app_normalizes_plain_db_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    captured: dict[str, str] = {}
    real_engine = create_database_engine(f"sqlite:///{tmp_path / 'real.db'}")

    def fake_create_database_engine(url: str):
        captured["url"] = url
        return real_engine

    monkeypatch.setattr("aap_migration.api.app.create_database_engine", fake_create_database_engine)

    app = create_app(db_url="relative.db")

    assert app.title == "AAP Bridge API"
    assert captured["url"] == "sqlite:///relative.db"

    real_engine.dispose()


def test_migrate_add_seq_id_backfills_legacy_jobs(tmp_path) -> None:
    db_path = tmp_path / "legacy-jobs.db"
    engine = create_database_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE api_jobs (
                    id VARCHAR(36) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    type VARCHAR(50) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO api_jobs (id, name, type, status, created_at)
                VALUES
                    ('job-1', 'First', 'test', 'completed', '2024-01-01 00:00:00'),
                    ('job-2', 'Second', 'test', 'completed', '2024-01-02 00:00:00')
                """
            )
        )

    _migrate_add_seq_id(engine)
    _migrate_add_seq_id(engine)

    with engine.connect() as conn:
        columns = [column["name"] for column in inspect(engine).get_columns("api_jobs")]
        rows = conn.execute(text("SELECT id, seq_id FROM api_jobs ORDER BY created_at")).all()

    assert "seq_id" in columns
    assert rows == [("job-1", 1), ("job-2", 2)]
    engine.dispose()


def test_recover_stale_jobs_marks_running_jobs_failed(session_factory: sessionmaker) -> None:
    session = session_factory()
    session.add_all(
        [
            JobRecord(id="running", seq_id=1, name="Run", type="demo", status="running"),
            JobRecord(id="waiting", seq_id=2, name="Wait", type="demo", status="waiting_for_input"),
        ]
    )
    session.commit()
    session.close()

    _recover_stale_jobs(session_factory)

    check = session_factory()
    running = check.get(JobRecord, "running")
    waiting = check.get(JobRecord, "waiting")
    assert running is not None and running.status == "failed"
    assert running.error == "Engine restarted — job did not complete"
    assert waiting is not None and waiting.status == "waiting_for_input"
    check.close()


def test_seed_connections_from_env_encryption_and_defaults(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker
) -> None:
    monkeypatch.setenv("SOURCE__URL", "https://source.example.com")
    monkeypatch.setenv("SOURCE__TOKEN", "source-token")
    monkeypatch.setenv("SOURCE__VERIFY_SSL", "false")
    monkeypatch.setenv("SOURCE__TIMEOUT", "45")
    monkeypatch.setenv("TARGET__URL", "https://target.example.com")
    monkeypatch.setenv("TARGET__TOKEN", "target-token")

    _seed_connections_from_env(session_factory)
    _seed_connections_from_env(session_factory)

    session = session_factory()
    connections = session.query(Connection).order_by(Connection.role).all()

    assert [conn.role for conn in connections] == ["source", "target"]
    assert crypto.decrypt_token(connections[0].token) == "source-token"
    assert connections[0].verify_ssl is False
    assert connections[0].timeout == 45
    assert crypto.decrypt_token(connections[1].token) == "target-token"
    assert connections[1].verify_ssl is True
    assert connections[1].timeout == 30
    session.close()


def test_app_startup_seeds_connections_and_sets_app_state(
    monkeypatch: pytest.MonkeyPatch, sqlite_db_url: str
) -> None:
    engine = create_database_engine(sqlite_db_url)
    from aap_migration.migration.models import Base

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    session.add(JobRecord(id="stale", seq_id=1, name="Stale", type="demo", status="running"))
    session.commit()
    session.close()
    engine.dispose()

    monkeypatch.setenv("SOURCE__URL", "https://source.example.com")
    monkeypatch.setenv("SOURCE__TOKEN", "source-token")

    app = create_app(db_url=sqlite_db_url)
    with TestClient(app):
        state = dependencies.get_app_state()
        session = state.db_session_factory()
        try:
            assert session.query(Connection).count() == 1
            stale = session.get(JobRecord, "stale")
            assert stale is not None and stale.status == "failed"
        finally:
            session.close()
