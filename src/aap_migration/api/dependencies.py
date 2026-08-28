"""FastAPI dependency injection providers."""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import Generator
from typing import TYPE_CHECKING

from fastapi import Header, HTTPException
from sqlalchemy import create_engine, pool
from sqlalchemy.orm import Session, sessionmaker

from aap_migration.api.models import ApiBase

if TYPE_CHECKING:
    from aap_migration.api.services.job_service import JobService

DEFAULT_API_DB_URL = "sqlite:///./api_state.db"


def get_db_url() -> str:
    """Return the API database URL (isolated from migration state)."""
    # Prefer explicit API DB var, fallback to default isolated file
    url = os.environ.get("API_STATE_DB_PATH") or os.environ.get("AAP_API_DB_URL")
    if url:
        if "://" not in url:
            raise RuntimeError(
                f"Database URL must be a full DSN (postgresql://...). Got {url!r}. Bare file paths are no longer supported."
            )
        return url
    return DEFAULT_API_DB_URL


class AppState:
    """Shared application state initialized at startup."""

    def __init__(
        self,
        db_url: str,
        session_factory: sessionmaker,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.db_url = db_url
        self.db_session_factory: sessionmaker = session_factory
        self.loop: asyncio.AbstractEventLoop | None = loop
        # Lazily created JobService (needs loop)
        self.job_service: JobService | None = None  # type: ignore[name-defined]

    def ensure_job_service(self) -> JobService:  # type: ignore[name-defined]
        from aap_migration.api.services.job_service import JobService

        if self.job_service is None:
            loop = self.loop or asyncio.get_event_loop()
            self.job_service = JobService(db_session_factory=self.db_session_factory, loop=loop)
        return self.job_service


_app_state: AppState | None = None


def set_app_state(state: AppState) -> None:
    global _app_state
    _app_state = state


def get_app_state() -> AppState:
    if _app_state is None:
        raise RuntimeError("AppState not initialized")
    return _app_state


def get_db() -> Generator[Session, None, None]:
    """Yield a DB session."""
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


def get_job_service() -> JobService:  # type: ignore[name-defined]
    return get_app_state().ensure_job_service()


# --- Auth ---


def _get_api_token() -> str | None:
    tok = os.environ.get("AAP_API_TOKEN", "").strip()
    return tok if tok else None


async def verify_api_token(authorization: str | None = Header(default=None)) -> None:
    """Verify Bearer token if AAP_API_TOKEN is set. If not set, allow all (dev mode)."""
    required = _get_api_token()
    if not required:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    provided = authorization[7:].strip()
    if not secrets.compare_digest(provided, required):
        raise HTTPException(status_code=401, detail="Invalid token")


def create_api_engine(db_url: str) -> object:
    """Create engine for API DB with proper pooling."""
    is_sqlite = db_url.startswith("sqlite")
    if is_sqlite:
        engine = create_engine(
            db_url, poolclass=pool.NullPool, connect_args={"check_same_thread": False}
        )
    else:
        engine = create_engine(db_url, pool_size=5, max_overflow=10, pool_pre_ping=True)
    # Ensure tables exist
    ApiBase.metadata.create_all(engine)
    # Lightweight migrations for existing DBs (add columns/indexes if missing)
    _migrate_api_db(engine)
    return engine


def _migrate_api_db(engine: object) -> None:
    """Apply additive migrations to existing API DB (idempotent)."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        # api_jobs: ensure seq_id column + index
        if insp.has_table("api_jobs"):
            cols = {c["name"] for c in insp.get_columns("api_jobs")}
            with engine.begin() as conn:
                if "seq_id" not in cols:
                    conn.execute(text("ALTER TABLE api_jobs ADD COLUMN seq_id INTEGER"))
                    # Backfill seq_id in creation order
                    try:
                        conn.execute(
                            text(
                                "UPDATE api_jobs SET seq_id = sub.rn FROM (SELECT id, ROW_NUMBER() OVER (ORDER BY created_at) AS rn FROM api_jobs) sub WHERE api_jobs.id = sub.id"
                            )
                        )
                    except Exception:
                        # Fallback: Python-side backfill if window func not supported (older sqlite)
                        pass
                    try:
                        conn.execute(
                            text(
                                "CREATE UNIQUE INDEX IF NOT EXISTS ix_api_jobs_seq_id ON api_jobs (seq_id)"
                            )
                        )
                    except Exception:
                        pass
                else:
                    # ensure index exists even if column existed
                    try:
                        conn.execute(
                            text(
                                "CREATE UNIQUE INDEX IF NOT EXISTS ix_api_jobs_seq_id ON api_jobs (seq_id)"
                            )
                        )
                    except Exception:
                        pass
    except Exception:
        # Migrations are best-effort; don't block startup
        pass
