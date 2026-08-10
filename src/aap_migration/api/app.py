"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import sessionmaker

from aap_migration.api.dependencies import AppState, get_db_url, set_app_state
from aap_migration.api.models import (  # noqa: F401 — registers tables
    Connection,
    JobRecord,
    MigrationPlan,
    MigrationPlanPhase,
    MigrationPlanPhaseOrg,
    MigrationPlanPhaseResourceType,
    MigrationPlanSource,
)
from aap_migration.api.services.job_service import JobService
from aap_migration.migration.database import init_database


def _migrate_add_seq_id(engine: object) -> None:
    """Add seq_id column to api_jobs if it doesn't exist, backfill existing rows."""
    from sqlalchemy import inspect, text
    from sqlalchemy.engine import Engine

    eng = engine if isinstance(engine, Engine) else None
    if eng is None:
        return
    insp = inspect(eng)
    if not insp.has_table("api_jobs"):
        return
    columns = [c["name"] for c in insp.get_columns("api_jobs")]
    if "seq_id" in columns:
        return
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE api_jobs ADD COLUMN seq_id INTEGER"))
        conn.execute(
            text(
                "UPDATE api_jobs SET seq_id = sub.rn FROM "
                "(SELECT id, ROW_NUMBER() OVER (ORDER BY created_at) AS rn FROM api_jobs) sub "
                "WHERE api_jobs.id = sub.id"
            )
        )
        conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ix_api_jobs_seq_id ON api_jobs (seq_id)")
        )


def _migrate_phase_resource_types(engine: object) -> None:
    """Add update_mode column and phase_resource_types table if missing."""
    from sqlalchemy import inspect, text
    from sqlalchemy.engine import Engine

    eng = engine if isinstance(engine, Engine) else None
    if eng is None:
        return
    insp = inspect(eng)
    if insp.has_table("api_migration_plan_phases"):
        columns = [c["name"] for c in insp.get_columns("api_migration_plan_phases")]
        if "update_mode" not in columns:
            with eng.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE api_migration_plan_phases "
                        "ADD COLUMN update_mode BOOLEAN NOT NULL DEFAULT FALSE"
                    )
                )


def create_app(db_url: str | None = None) -> FastAPI:
    from aap_migration.api.crypto import ensure_encryption_key_configured

    ensure_encryption_key_configured()

    effective_url: str = db_url or get_db_url()

    if not effective_url.startswith(("sqlite://", "postgresql://", "mysql://")):
        raise ValueError(
            "Database URL must be a full DSN (postgresql://...). "
            f"Got {effective_url!r}. Bare file paths are no longer supported."
        )

    engine = init_database(effective_url)
    _migrate_phase_resource_types(engine)
    _migrate_add_seq_id(engine)

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    job_service = JobService(db_session_factory=session_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        state = AppState(session_factory, job_service, loop)
        set_app_state(state)

        _seed_connections_from_env(session_factory)
        _recover_stale_jobs(session_factory)

        yield

        engine.dispose()

    app = FastAPI(
        title="AAP Bridge API",
        version="0.5.4",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from aap_migration.api import websocket
    from aap_migration.api.routers import (
        analysis,
        connections,
        iam,
        jobs,
        migration,
        operations,
        planner,
        resources,
        settings,
        sizing,
    )

    app.include_router(connections.router, prefix="/api", tags=["connections"])
    app.include_router(resources.router, prefix="/api", tags=["resources"])
    app.include_router(operations.router, prefix="/api", tags=["operations"])
    app.include_router(migration.router, prefix="/api", tags=["migration"])
    app.include_router(planner.router, prefix="/api", tags=["planner"])
    app.include_router(jobs.router, prefix="/api", tags=["jobs"])
    app.include_router(analysis.router, prefix="/api", tags=["analysis"])
    app.include_router(iam.router, prefix="/api", tags=["iam"])
    app.include_router(sizing.router, prefix="/api", tags=["sizing"])
    app.include_router(settings.router, prefix="/api", tags=["settings"])
    app.include_router(websocket.router)

    return app


def _recover_stale_jobs(session_factory: sessionmaker) -> None:
    """Mark DB jobs stuck in 'running' or 'waiting_for_input' on startup."""
    session = session_factory()
    try:
        from sqlalchemy import update

        from aap_migration.api.services.job_service import JobStatus

        running_stmt = (
            update(JobRecord)
            .where(JobRecord.status == JobStatus.RUNNING)
            .values(status=JobStatus.FAILED, error="Engine restarted — job did not complete")
        )
        session.execute(running_stmt)

        # waiting_for_input jobs survive restarts — they are intentionally
        # paused and the resume endpoint will re-execute the phase.

        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _is_placeholder_env_value(value: str | None) -> bool:
    """Return True for empty or template-style SOURCE__/TARGET__ values."""
    if value is None:
        return True
    cleaned = value.strip().strip('"').strip("'")
    if not cleaned:
        return True
    # Template markers from .env.example / container/.env
    if "<" in cleaned and ">" in cleaned:
        return True
    placeholders = {
        "xxxxx",
        "xxxxxx",
        "changeme",
        "your-token",
        "your-source-token",
        "your-target-token",
    }
    return cleaned.lower() in placeholders


def _seed_connections_from_env(session_factory: sessionmaker) -> None:
    """Bootstrap missing connections from SOURCE__/TARGET__ env vars.

    Existing DB connections are never updated or replaced. Each role is seeded
    independently only when that role is absent, and placeholder env values are
    ignored so template `.env` files cannot wipe UI-configured instances.
    """
    from aap_migration.api.crypto import encrypt_token

    session = session_factory()
    try:
        existing = session.query(Connection).all()
        has_source = any(c.role == "source" for c in existing)
        has_destination = any(c.role in ("destination", "target") for c in existing)

        # (db_role, env_prefix, default_type)
        roles_to_seed: list[tuple[str, str, str, bool]] = [
            ("source", "SOURCE__", "awx", has_source),
            ("destination", "TARGET__", "aap", has_destination),
        ]

        created = False
        for role, prefix, default_type, already_present in roles_to_seed:
            if already_present:
                continue
            url = os.environ.get(f"{prefix}URL")
            token = os.environ.get(f"{prefix}TOKEN")
            if _is_placeholder_env_value(url) or _is_placeholder_env_value(token):
                continue
            assert url is not None and token is not None  # narrowed by placeholder check
            conn = Connection(
                name=f"{role.capitalize()} AAP",
                url=url.strip().strip('"').strip("'"),
                token=encrypt_token(token.strip().strip('"').strip("'")),
                type=default_type,
                role=role,
                verify_ssl=os.environ.get(f"{prefix}VERIFY_SSL", "true").lower() == "true",
                timeout=int(os.environ.get(f"{prefix}TIMEOUT", "30")),
            )
            session.add(conn)
            created = True

        if created:
            session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()
