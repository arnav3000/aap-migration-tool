"""Persistence for API-managed AAP connection settings.

Connections are stored in a dedicated SQLite database (default
``./api_state.db``, overridable via ``AAP_BRIDGE_API_DB``), separate from the
migration state database. Tokens are stored encrypted (see
:mod:`aap_migration.api.security`).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for API models."""


class ApiConnection(Base):
    """A stored AAP endpoint (source or target)."""

    __tablename__ = "api_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # source | target
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    verify_ssl: Mapped[bool] = mapped_column(default=True, nullable=False)
    timeout: Mapped[int] = mapped_column(default=30, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class ApiActiveConfig(Base):
    """Singleton row pointing at the active source/target connections."""

    __tablename__ = "api_active_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


def api_db_path() -> str:
    """Resolve the API database path from the environment."""
    return os.environ.get("AAP_BRIDGE_API_DB", "./api_state.db")


def _connect_args(db_path: str) -> dict:
    if db_path.startswith("sqlite"):
        return {"check_same_thread": False, "timeout": 30}
    return {}


def _configure_sqlite(dbapi_conn: object, record: object) -> None:
    """Enable WAL + busy timeout so API threads and workers share the DB."""
    try:
        cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.close()
    except Exception:
        pass


def get_api_engine(db_path: str | None = None) -> Engine:
    """Create (or return) a SQLAlchemy engine for the API database."""
    path = db_path or api_db_path()
    url = path if "://" in path else f"sqlite:///{path}"
    engine = create_engine(url, connect_args=_connect_args(url), future=True)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_sqlite)
    return engine


def init_api_db(db_path: str | None = None) -> str:
    """Create API tables if they do not exist. Returns the DB path used."""
    path = db_path or api_db_path()
    # Ensure parent directory exists for file-based sqlite DBs
    if "://" not in path:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
    engine = get_api_engine(path)
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@contextmanager
def api_session(db_path: str | None = None) -> Iterator[Session]:
    """Yield a SQLAlchemy session with commit/rollback handling."""
    engine = get_api_engine(db_path)
    factory = sessionmaker(bind=engine, future=True)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()
