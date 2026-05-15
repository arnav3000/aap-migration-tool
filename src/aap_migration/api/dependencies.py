"""FastAPI dependency injection providers."""

from __future__ import annotations

import asyncio
from collections.abc import Generator

from sqlalchemy.orm import Session, sessionmaker

from aap_migration.api.services.job_service import JobService


class AppState:
    """Shared application state initialized at startup."""

    def __init__(
        self,
        db_session_factory: sessionmaker,
        job_service: JobService,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.db_session_factory = db_session_factory
        self.job_service = job_service
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
