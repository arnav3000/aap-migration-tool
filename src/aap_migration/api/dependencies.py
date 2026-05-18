"""FastAPI dependency injection providers."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Generator

from sqlalchemy.orm import Session, sessionmaker

from aap_migration.api.services.job_service import JobService

_DEFAULT_PG_URL = "postgresql://aap_user:changeme@localhost:5432/aap_migration"
_SQLITE_FALLBACK = "sqlite:///aap_bridge.db"


def get_db_url() -> str:
    """Return the database URL from env, defaulting to PostgreSQL."""
    return os.environ.get("MIGRATION_STATE_DB_PATH", _DEFAULT_PG_URL)


class AppState:
    """Shared application state initialized at startup."""

    def __init__(
        self,
        db_session_factory: sessionmaker,
        job_service: JobService | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.db_session_factory = db_session_factory
        self.job_service = job_service or JobService(db_session_factory=db_session_factory)
        self.loop = loop


_app_state: AppState | None = None


def set_app_state(state: AppState) -> None:
    global _app_state
    _app_state = state


def get_app_state() -> AppState:
    if _app_state is None:
        raise RuntimeError("AppState not initialized")
    return _app_state


def get_db() -> Generator[Session, None, None]:
    state = get_app_state()
    session = state.db_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_job_service() -> JobService:
    return get_app_state().job_service
