"""
Database initialization and connection management utilities.

This module provides functions for initializing the migration database,
managing connections, and creating sessions with proper pooling and
thread safety.
"""

import os
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, pool, text
from sqlalchemy.exc import OperationalError as _SAOperationalError
from sqlalchemy.orm import Session, sessionmaker

from aap_migration.client.exceptions import ConfigurationError, StateError
from aap_migration.migration.models import Base
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)

# Global engine and session factory (initialized on first use)
# Legacy singletons (kept for callers without an explicit URL) plus a
# per-URL registry so concurrent API jobs with different state DBs never
# retarget each other's sessions. All mutations hold _registry_lock.
_engine: Engine | None = None
_SessionFactory: sessionmaker | None = None
_engines: dict[str, Engine] = {}
_factories: dict[str, sessionmaker] = {}
_registry_lock = threading.Lock()
# Legacy singletons are frozen on first init: later init_database() calls for
# a different URL register in the per-URL registry but never retarget the
# globals, so callers without an explicit URL keep a stable binding.
_legacy_url: str | None = None
_MAX_REGISTRY_ENTRIES = 64


def _adopt_legacy_locked(database_url: str) -> None:
    """Point legacy globals at *database_url* iff it is the frozen first URL.

    Legacy singletons are frozen to the first URL ever initialized; later
    URLs register per-URL but never retarget the globals. Must hold
    ``_registry_lock``.
    """
    global _engine, _SessionFactory, _legacy_url
    if _legacy_url is None:
        _legacy_url = database_url
        _engine = _engines[database_url]
        _SessionFactory = _factories[database_url]
    elif database_url == _legacy_url:
        _engine = _engines[database_url]
        _SessionFactory = _factories[database_url]
    else:
        logger.warning(
            "init_database retarget ignored: legacy globals frozen to first URL",
            first_url=_legacy_url,
            new_url=database_url,
        )


def _configure_sqlite(dbapi_conn: Any, connection_record: Any) -> None:
    """Enable WAL + busy timeout + FK so threads share state DBs safely."""
    try:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception as exc:
        # Fail open (the DB still works) but log loudly: without WAL and
        # the busy timeout, concurrent jobs hit "database is locked"
        # immediately instead of waiting, and the root cause would never
        # appear in logs.
        logger.warning("sqlite_pragmas_failed; running without WAL/busy-timeout", error=str(exc))


def create_database_engine(
    database_url: str,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout: int = 30,
    pool_recycle: int = 3600,
) -> Engine:
    """
    Create a SQLAlchemy engine with appropriate settings.

    Args:
        database_url: Database connection URL (sqlite:/// or postgresql://)
        echo: Whether to log SQL statements (useful for debugging)
        pool_size: Number of connections to maintain in the pool
        max_overflow: Maximum number of connections that can be created beyond pool_size
        pool_timeout: Timeout for getting a connection from the pool (seconds)
        pool_recycle: Recycle connections after this many seconds (prevents stale connections)

    Returns:
        SQLAlchemy Engine instance

    Raises:
        ConfigurationError: If database URL is invalid
    """
    if not database_url:
        raise ConfigurationError("Database URL cannot be empty")

    try:
        # Determine database type
        is_sqlite = database_url.startswith("sqlite")
        is_postgresql = database_url.startswith("postgresql")

        # Configure engine based on database type
        if is_sqlite:
            # SQLite-specific configuration
            # Use NullPool for SQLite to avoid threading issues
            engine = create_engine(
                database_url,
                echo=echo,
                poolclass=pool.NullPool,  # No connection pooling for SQLite
                connect_args={"check_same_thread": False, "timeout": 30},
            )
            # WAL + busy timeout + FK so concurrent jobs share DBs safely.
            event.listen(engine, "connect", _configure_sqlite)

        elif is_postgresql:
            # PostgreSQL-specific configuration
            # Use QueuePool for PostgreSQL (default, with custom settings)
            engine = create_engine(
                database_url,
                echo=echo,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout=pool_timeout,
                pool_pre_ping=True,  # Verify connections before using
                pool_recycle=pool_recycle,
            )

        else:
            # Generic configuration for other databases
            engine = create_engine(
                database_url,
                echo=echo,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout=pool_timeout,
                pool_pre_ping=True,
            )

        logger.info(
            "Database engine created",
            database_type="sqlite" if is_sqlite else "postgresql" if is_postgresql else "other",
            pool_size=pool_size if not is_sqlite else "NullPool",
        )

        return engine

    except Exception as e:
        logger.error("Failed to create database engine", error=str(e), database_url=database_url)
        raise ConfigurationError(f"Failed to create database engine: {e}") from e


def init_database(
    database_url: str,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout: int = 30,
    pool_recycle: int = 3600,
) -> Engine:
    """
    Initialize the migration database.

    Creates all tables if they don't exist. This is idempotent and safe
    to call multiple times. Engines are kept in a per-URL registry guarded
    by a lock so concurrent API jobs with different state DBs never flip
    each other's sessions; the legacy globals are frozen to the first URL
    for callers that omit one.

    Args:
        database_url: Database connection URL
        echo: Whether to log SQL statements
        pool_size: Number of connections to maintain in the pool
        max_overflow: Maximum number of connections that can be created beyond pool_size
        pool_timeout: Timeout for getting a connection from the pool (seconds)
        pool_recycle: Recycle connections after this many seconds

    Returns:
        SQLAlchemy Engine instance

    Raises:
        ConfigurationError: If database initialization fails
    """
    global _engine, _SessionFactory, _legacy_url

    try:
        with _registry_lock:
            existing = _engines.get(database_url)
        if existing is not None:
            # Ensure tables exist for this URL without rebuilding. Runs
            # outside the lock: create_all round-trips the DB and must not
            # stall every thread's get_engine/get_session path.
            Base.metadata.create_all(existing)
            with _registry_lock:
                current = _engines.get(database_url)
                if current is not None:
                    _adopt_legacy_locked(database_url)
                    return current
                # Evicted mid-flight: re-register our handle (a disposed
                # engine re-opens its pool on next use, so this stays valid).
                _engines[database_url] = existing
                _factories.setdefault(
                    database_url, sessionmaker(bind=existing, expire_on_commit=False)
                )
                _adopt_legacy_locked(database_url)
                return existing
        # Create engine outside the lock (may do I/O), then publish.
        engine = create_database_engine(
            database_url,
            echo=echo,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
        )

        # Create all tables
        Base.metadata.create_all(engine)

        # Create session factory
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        to_dispose: Engine | None = None
        with _registry_lock:
            # Another thread may have won the race; reuse theirs and
            # dispose ours (outside the lock) to avoid leaking a pool.
            if database_url in _engines:
                to_dispose = engine
                _adopt_legacy_locked(database_url)
                published = _engines[database_url]
            else:
                if len(_engines) >= _MAX_REGISTRY_ENTRIES:
                    victim = next((u for u in _engines if u != _legacy_url), None)
                    if victim is None:
                        # Every entry is legacy-pinned: refuse to evict the
                        # frozen engine legacy callers are bound to; grow
                        # past the cap instead of disposing a live engine.
                        logger.warning(
                            "engine registry full and all entries legacy-pinned; "
                            "growing past cap instead of evicting",
                        )
                    else:
                        to_dispose = _engines.pop(victim)
                        _factories.pop(victim, None)
                        logger.warning(
                            "engine registry full; evicted oldest non-legacy entry",
                            evicted_url=victim,
                        )
                _engines[database_url] = engine
                _factories[database_url] = factory
                _adopt_legacy_locked(database_url)
                published = engine

        # Dispose outside the lock: dispose() waits for checked-out
        # connections (up to pool_timeout) and must not stall the registry.
        if to_dispose is not None:
            try:
                to_dispose.dispose()
            except Exception:
                pass

        logger.info(
            "Database initialized successfully",
            database_url=database_url,
            tables=len(Base.metadata.tables),
        )

        return published

    except Exception as e:
        logger.error("Failed to initialize database", error=str(e), database_url=database_url)
        raise ConfigurationError(f"Failed to initialize database: {e}") from e


def get_engine(database_url: str | None = None, echo: bool = False) -> Engine:
    """
    Get the database engine for *database_url* (per-URL registry).

    If the engine hasn't been initialized yet, this will initialize it.
    If database_url is not provided, uses the frozen first/legacy engine
    (legacy behavior for callers without an explicit URL).

    Args:
        database_url: Database connection URL (optional if already initialized)
        echo: Whether to log SQL statements

    Returns:
        SQLAlchemy Engine instance

    Raises:
        ConfigurationError: If engine is not initialized and no URL provided
    """
    global _engine

    if database_url is not None:
        with _registry_lock:
            engine = _engines.get(database_url)
        if engine is not None:
            return engine
        init_database(database_url, echo=echo)
        with _registry_lock:
            engine = _engines.get(database_url)
        assert engine is not None, "Engine should be initialized by init_database()"
        return engine

    if _engine is None:
        raise ConfigurationError(
            "Database engine not initialized. Call init_database() first or provide database_url."
        )

    # Assert for type checker - init_database() guarantees _engine is not None
    assert _engine is not None, "Engine should be initialized by init_database()"
    return _engine


def get_session_factory() -> sessionmaker:
    """
    Get the global session factory.

    Returns:
        SQLAlchemy sessionmaker instance

    Raises:
        ConfigurationError: If session factory is not initialized
    """
    global _SessionFactory

    if _SessionFactory is None:
        raise ConfigurationError("Session factory not initialized. Call init_database() first.")

    return _SessionFactory


@contextmanager
def get_session(database_url: str | None = None) -> Generator[Session, None, None]:
    """
    Context manager for database sessions.

    Automatically commits on success and rolls back on exception.
    Always closes the session when done.

    Usage:
        with get_session() as session:
            session.add(obj)
            session.commit()  # Optional, will auto-commit on exit

    Args:
        database_url: Database connection URL (optional if already initialized)

    Yields:
        SQLAlchemy Session instance

    Raises:
        StateError: If database operation fails
    """
    # Ensure engine is initialized and bind to this URL's factory (never
    # the legacy global, which may point at another job's DB).
    get_engine(database_url)
    if database_url is not None:
        with _registry_lock:
            session_factory = _factories.get(database_url)
        if session_factory is None:
            raise ConfigurationError("Session factory not initialized. Call init_database() first.")
    else:
        # Get session factory
        session_factory = get_session_factory()

    # Create session
    session = session_factory()

    try:
        yield session
        try:
            session.commit()
        except (_SAOperationalError, sqlite3.OperationalError) as exc:
            # Fail loudly on sqlite locked/busy: the yielded transaction's
            # writes must never be rolled back and then re-committed as an
            # empty (successful) transaction. Roll back and raise so the
            # caller retries the whole unit of work instead.
            try:
                session.rollback()
            except Exception:
                pass
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                raise StateError(
                    "Database is locked by a concurrent reader/writer; retry the operation"
                ) from exc
            raise
        logger.debug("Database session committed successfully")

    except Exception as e:
        session.rollback()
        logger.error("Database session rolled back due to error", error=str(e))
        raise StateError(f"Database operation failed: {e}") from e

    finally:
        session.close()


def reset_database(database_url: str) -> None:
    """
    Drop all tables and recreate them.

    WARNING: This destroys all data in the database! Only use for testing
    or when you explicitly want to reset the migration state.

    Args:
        database_url: Database connection URL

    Raises:
        ConfigurationError: If database reset fails
    """
    try:
        engine = create_database_engine(database_url)

        # Drop all tables
        Base.metadata.drop_all(engine)
        logger.warning("All database tables dropped", database_url=database_url)

        # Recreate all tables
        Base.metadata.create_all(engine)
        logger.info("Database tables recreated", database_url=database_url)

    except Exception as e:
        logger.error("Failed to reset database", error=str(e), database_url=database_url)
        raise ConfigurationError(f"Failed to reset database: {e}") from e
    finally:
        try:
            engine.dispose()
        except Exception:
            pass


def validate_database_connection(database_url: str) -> bool:
    """
    Validate that a database connection can be established.

    Args:
        database_url: Database connection URL

    Returns:
        True if connection successful, False otherwise
    """
    try:
        engine = create_database_engine(database_url)
        try:
            with engine.connect() as conn:
                # Execute a simple query to verify connection
                conn.execute(text("SELECT 1"))
        finally:
            try:
                engine.dispose()
            except Exception:
                pass
        logger.info("Database connection validated successfully", database_url=database_url)
        return True

    except Exception as e:
        logger.error(
            "Database connection validation failed", error=str(e), database_url=database_url
        )
        return False


def get_database_size(database_url: str) -> int:
    """
    Get the size of the database file (SQLite only).

    Args:
        database_url: Database connection URL (sqlite only)

    Returns:
        Database size in bytes, or 0 if not applicable/not found

    Raises:
        ValueError: If not a SQLite database
    """
    if not database_url.startswith("sqlite"):
        raise ValueError("Database size check only supported for SQLite databases")

    # Extract file path from URL
    # Format: sqlite:///path/to/file.db or sqlite:////absolute/path/to/file.db
    db_path = database_url.replace("sqlite:///", "")

    if not os.path.exists(db_path):
        return 0

    size_bytes = os.path.getsize(db_path)
    logger.debug("Database size checked", database_path=db_path, size_bytes=size_bytes)

    return size_bytes


def create_database_backup(database_url: str, backup_path: str) -> None:
    """
    Create a backup of the SQLite database (SQLite only).

    Args:
        database_url: Database connection URL (sqlite only)
        backup_path: Path where backup should be saved

    Raises:
        ValueError: If not a SQLite database
        ConfigurationError: If backup fails
    """
    import shutil

    if not database_url.startswith("sqlite"):
        raise ValueError("Database backup only supported for SQLite databases")

    try:
        # Extract file path from URL
        db_path = database_url.replace("sqlite:///", "")

        if not os.path.exists(db_path):
            raise ConfigurationError(f"Database file not found: {db_path}")

        # Create backup directory if it doesn't exist
        backup_dir = Path(backup_path).parent
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Copy database file
        shutil.copy2(db_path, backup_path)

        logger.info("Database backup created", source=db_path, backup=backup_path)

    except Exception as e:
        logger.error("Failed to create database backup", error=str(e))
        raise ConfigurationError(f"Failed to create database backup: {e}") from e
