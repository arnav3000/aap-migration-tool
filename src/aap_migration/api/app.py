"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from aap_migration.api.dependencies import AppState, set_app_state
from aap_migration.api.models import (  # noqa: F401 — registers tables
    Connection,
    JobRecord,
    MigrationPlan,
    MigrationPlanPhase,
    MigrationPlanPhaseOrg,
    MigrationPlanSource,
)
from aap_migration.api.services.job_service import JobService
from aap_migration.migration.database import create_database_engine
from aap_migration.migration.models import Base


def _cors_allow_origins() -> list[str]:
    """Comma-separated list in AAP_CORS_ORIGINS; defaults for local UI dev."""
    raw = os.environ.get("AAP_CORS_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return ["http://localhost:5173", "http://localhost:8080"]


def _migrate_add_seq_id(engine: Engine) -> None:
    """Add seq_id column to api_jobs if it doesn't exist, backfill existing rows."""
    from sqlalchemy import text

    insp = inspect(engine)
    if not insp.has_table("api_jobs"):
        return
    columns = [c["name"] for c in insp.get_columns("api_jobs")]
    if "seq_id" in columns:
        return

    dialect = engine.dialect.name
    pg_backfill = (
        "UPDATE api_jobs SET seq_id = sub.rn FROM "
        "(SELECT id, ROW_NUMBER() OVER (ORDER BY created_at) AS rn FROM api_jobs) sub "
        "WHERE api_jobs.id = sub.id"
    )
    portable_backfill = (
        "UPDATE api_jobs SET seq_id = ("
        "SELECT sub.rn FROM ("
        "SELECT id, ROW_NUMBER() OVER (ORDER BY created_at) AS rn FROM api_jobs"
        ") AS sub WHERE sub.id = api_jobs.id)"
    )

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE api_jobs ADD COLUMN seq_id INTEGER"))
        stmt = pg_backfill if dialect == "postgresql" else portable_backfill
        conn.execute(text(stmt))
        conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ix_api_jobs_seq_id ON api_jobs (seq_id)")
        )


def create_app(db_url: str | None = None) -> FastAPI:
    effective_url: str = (
        db_url
        or os.environ.get("MIGRATION_STATE_DB_PATH", "sqlite:///aap_bridge.db")
        or "sqlite:///aap_bridge.db"
    )

    if not effective_url.startswith(("sqlite", "postgresql", "mysql")):
        effective_url = f"sqlite:///{effective_url}"

    engine = create_database_engine(effective_url)
    Base.metadata.create_all(engine)
    _migrate_add_seq_id(engine)

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    job_service = JobService(db_session_factory=session_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        state = AppState(session_factory, job_service, loop)
        set_app_state(state)

        _seed_connections_from_env(session_factory)

        yield

        engine.dispose()

    app = FastAPI(
        title="AAP Migration Tool API",
        version="0.5.4",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_allow_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    from aap_migration.api import websocket
    from aap_migration.api.routers import (
        analysis,
        connections,
        jobs,
        migration,
        operations,
        planner,
        resources,
        sizing,
    )

    app.include_router(connections.router, prefix="/api", tags=["connections"])
    app.include_router(resources.router, prefix="/api", tags=["resources"])
    app.include_router(operations.router, prefix="/api", tags=["operations"])
    app.include_router(migration.router, prefix="/api", tags=["migration"])
    app.include_router(planner.router, prefix="/api", tags=["planner"])
    app.include_router(jobs.router, prefix="/api", tags=["jobs"])
    app.include_router(analysis.router, prefix="/api", tags=["analysis"])
    app.include_router(sizing.router, prefix="/api", tags=["sizing"])
    app.include_router(websocket.router)

    return app


def _seed_connections_from_env(session_factory: sessionmaker) -> None:
    """Auto-create connections from SOURCE__*/TARGET__* env vars if DB is empty."""
    from aap_migration.api.crypto import encrypt_token

    session = session_factory()
    try:
        if session.query(Connection).count() > 0:
            return

        for role, prefix in [("source", "SOURCE__"), ("destination", "TARGET__")]:
            url = os.environ.get(f"{prefix}URL")
            token = os.environ.get(f"{prefix}TOKEN")
            if url and token:
                conn = Connection(
                    name=f"{role.capitalize()} AAP",
                    url=url,
                    token=encrypt_token(token),
                    role=role,
                    verify_ssl=os.environ.get(f"{prefix}VERIFY_SSL", "true").lower() == "true",
                    timeout=int(os.environ.get(f"{prefix}TIMEOUT", "30")),
                )
                session.add(conn)
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()
