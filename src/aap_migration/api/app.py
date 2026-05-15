import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aap_migration.api.dependencies import set_app_state
from aap_migration.api.models import Base, Connection
from aap_migration.api.routers import (
    analysis,
    connections,
    jobs,
    migration,
    operations,
    resources,
    sizing,
)
from aap_migration.api.schemas import ConnectionCreate
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.job_service import JobService
from aap_migration.api.websocket import router as ws_router

logger = logging.getLogger(__name__)

_db_url: str = ""


@dataclass
class AppState:
    db_session_factory: sessionmaker[Session] = field(init=False)
    job_service: JobService = field(init=False)
    loop: asyncio.AbstractEventLoop = field(init=False)

    def __post_init__(self) -> None:
        self.job_service = JobService()


_PLACEHOLDER_PATTERNS = ("<source_aap_url>", "<target_aap_url>", "xxxxx", "xxxxxx")


def _detect_type(url: str, version: str) -> str:
    """Infer 'aap' vs 'awx' from the URL path or version string."""
    if "/api/controller/" in url:
        return "aap"
    if version and version.startswith("2.") and version != "2.x":
        return "aap"
    return "awx"


def _seed_connections_from_env(db: Session) -> None:
    existing = db.query(Connection).count()
    if existing:
        return

    svc = ConnectionService(db)
    seeded: list[str] = []

    for prefix, role in (("SOURCE", "source"), ("TARGET", "destination")):
        url = os.environ.get(f"{prefix}__URL", "").strip().strip('"')
        token = os.environ.get(f"{prefix}__TOKEN", "").strip().strip('"')
        version = os.environ.get(f"{prefix}__VERSION", "").strip().strip('"')
        verify_ssl_raw = os.environ.get(f"{prefix}__VERIFY_SSL", "true").strip().strip('"')
        verify_ssl = verify_ssl_raw.lower() not in ("false", "0", "no")

        if not url or any(p in url for p in _PLACEHOLDER_PATTERNS):
            continue
        if not token or any(p == token for p in _PLACEHOLDER_PATTERNS):
            continue

        conn_type = _detect_type(url, version)
        name = f"{role.title()} (from .env)"

        svc.create(
            ConnectionCreate(
                name=name,
                type=conn_type,
                role=role,
                url=url,
                token=token,
                verify_ssl=verify_ssl,
            )
        )
        seeded.append(f"{role} ({conn_type}) -> {url}")

    if seeded:
        logger.info("Seeded %d connection(s) from .env: %s", len(seeded), "; ".join(seeded))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    engine = create_engine(_db_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)

    from aap_migration.migration.models import Base as MigrationBase

    MigrationBase.metadata.create_all(engine)

    from aap_migration.analysis.models import (
        AnalyzedOrganization,  # noqa: F401 — triggers table registration
    )

    MigrationBase.metadata.create_all(engine)

    state = AppState()
    state.db_session_factory = sessionmaker(bind=engine)
    state.loop = asyncio.get_running_loop()
    set_app_state(state)

    with state.db_session_factory() as db:
        _seed_connections_from_env(db)

    yield
    engine.dispose()


def create_app(db_url: str = "") -> FastAPI:
    global _db_url
    _db_url = db_url or os.environ.get("MIGRATION_STATE_DB_PATH", "")

    app = FastAPI(
        title="AAP Bridge",
        description="Web API for AAP Bridge migration tool",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(connections.router, prefix="/api")
    app.include_router(resources.router, prefix="/api")
    app.include_router(operations.router, prefix="/api")
    app.include_router(migration.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(analysis.router, prefix="/api")
    app.include_router(sizing.router, prefix="/api")
    app.include_router(ws_router)

    return app
