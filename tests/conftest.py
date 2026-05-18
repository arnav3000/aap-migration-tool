from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from aap_migration.api import crypto, dependencies
from aap_migration.api import models as api_models  # noqa: F401
from aap_migration.api.app import create_app
from aap_migration.migration.database import create_database_engine
from aap_migration.migration.models import Base


@pytest.fixture(autouse=True)
def reset_global_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AAP_TOKEN_ENCRYPTION_KEY", "unit-test-key")
    for name in (
        "SOURCE__URL",
        "SOURCE__TOKEN",
        "SOURCE__VERIFY_SSL",
        "SOURCE__TIMEOUT",
        "TARGET__URL",
        "TARGET__TOKEN",
        "TARGET__VERIFY_SSL",
        "TARGET__TIMEOUT",
        "MIGRATION_STATE_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    crypto._fernet = None
    dependencies._app_state = None

    from aap_migration.migration import database as migration_database

    if migration_database._engine is not None:
        migration_database._engine.dispose()
    migration_database._engine = None
    migration_database._SessionFactory = None

    yield

    crypto._fernet = None
    dependencies._app_state = None
    if migration_database._engine is not None:
        migration_database._engine.dispose()
    migration_database._engine = None
    migration_database._SessionFactory = None


@pytest.fixture
def sqlite_db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def session_factory(sqlite_db_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(sqlite_db_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def db_session(session_factory: sessionmaker) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def api_app(sqlite_db_url: str) -> FastAPI:
    return create_app(db_url=sqlite_db_url)


@pytest.fixture
def client(api_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(api_app) as test_client:
        yield test_client
