"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import sessionmaker

from aap_migration import __version__
from aap_migration.api.dependencies import (
    AppState,
    create_api_engine,
    get_db_url,
    set_app_state,
)
from aap_migration.api.routers import (
    analysis,
    connections,
    health,
    iam,
    jobs,
    migration,
    operations,
    planner,
    resources,
    settings,
    sizing,
    validate,
)
from aap_migration.api.websocket import router as websocket_router


def _get_cors_origins() -> list[str]:
    raw = os.environ.get("AAP_CORS_ORIGINS", "").strip()
    if not raw:
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db_url = get_db_url()
    # Validate DSN
    if "://" not in db_url:
        raise RuntimeError(
            f"Database URL must be a full DSN (postgresql://...). Got {db_url!r}. Bare file paths are no longer supported."
        )
    engine = create_api_engine(db_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    loop = asyncio.get_running_loop()
    state = AppState(db_url=db_url, session_factory=session_factory, loop=loop)
    # Recover stale jobs
    try:
        state.ensure_job_service().recover_stale_jobs()
    except Exception:
        pass
    # Seed connections from env if any (best-effort)
    try:
        _seed_connections_from_env(session_factory)
    except Exception:
        pass
    set_app_state(state)
    yield
    # Cleanup
    try:
        engine.dispose()
    except Exception:
        pass


def _seed_connections_from_env(session_factory: sessionmaker) -> None:
    """Seed api_connections from SOURCE__URL / TARGET__URL env if present and not already seeded."""
    from aap_migration.api.crypto import encrypt_token
    from aap_migration.api.models import Connection

    source_url = os.environ.get("SOURCE__URL", "").strip()
    target_url = os.environ.get("TARGET__URL", "").strip()
    source_token = os.environ.get("SOURCE__TOKEN", "").strip()
    target_token = os.environ.get("TARGET__TOKEN", "").strip()
    if not source_url and not target_url:
        return
    # Need encryption key for seeding tokens
    has_key = bool(os.environ.get("AAP_TOKEN_ENCRYPTION_KEY", "").strip())
    session = session_factory()
    try:
        existing = {c.role for c in session.query(Connection).all()}
        to_add = []
        if source_url and source_token and "source" not in existing:
            tok = encrypt_token(source_token) if has_key else source_token
            to_add.append(Connection(name="source-env", url=source_url, token=tok, role="source"))
        if target_url and target_token and "target" not in existing:
            tok = encrypt_token(target_token) if has_key else target_token
            to_add.append(Connection(name="target-env", url=target_url, token=tok, role="target"))
        for c in to_add:
            session.add(c)
        if to_add:
            session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        session.close()


def create_app(db_url: str | None = None) -> FastAPI:
    """Create and return the FastAPI app.

    Args:
        db_url: Optional override for API DB URL (useful for tests).
    """
    # Allow test injection via env or param
    if db_url:
        os.environ["API_STATE_DB_PATH"] = db_url

    app = FastAPI(
        title="AAP Bridge API",
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_get_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers under /api
    app.include_router(health.router, prefix="/api", tags=["health"])
    app.include_router(resources.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(connections.router, prefix="/api")
    app.include_router(migration.router, prefix="/api")
    app.include_router(analysis.router, prefix="/api")
    app.include_router(operations.router, prefix="/api")
    app.include_router(planner.router, prefix="/api")
    app.include_router(iam.router, prefix="/api")
    app.include_router(validate.router, prefix="/api")
    app.include_router(sizing.router, prefix="/api")
    app.include_router(settings.router, prefix="/api")
    app.include_router(websocket_router)

    return app
