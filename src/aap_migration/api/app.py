"""FastAPI application factory for the AAP Bridge REST API."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from aap_migration import __version__
from aap_migration.api._paths import API_V1_PREFIX
from aap_migration.api.models import init_api_db
from aap_migration.api.routers import config, connections, jobs, system
from aap_migration.api.security import require_api_key

log = logging.getLogger("aap_migration.api.app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup validation + shutdown drain for background jobs.

    Startup probes the API DB and job directory for writability. Probe
    failures mark the server degraded (submits fail fast with 503) unless
    the explicit dev opt-in ``AAP_BRIDGE_ALLOW_DEGRADED_STARTUP=1`` is set,
    which keeps the historical warn-only behavior for tests/local dev.
    Shutdown stops submissions and bounded-waits for the FIFO worker
    (``AAP_BRIDGE_DRAIN_SECS``, default 30s), logging leftovers; in-memory
    FIFO state is not durable across restarts (documented).
    """
    from aap_migration.api.jobs import set_startup_degraded

    degraded: list[str] = []
    try:
        db_path = init_api_db()
        if "://" not in db_path:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log.warning("API DB startup check failed: %s", exc)
        degraded.append("api-db: storage-unhealthy")
    job_dir = os.environ.get("AAP_BRIDGE_JOB_DIR") or "./api_jobs"
    try:
        # Same probe the readiness endpoint uses (routers.system): one
        # definition of writable for startup and readiness.
        from aap_migration.api.routers.system import probe_dir_writable

        probe_dir_writable(job_dir)
    except Exception as exc:
        log.warning("job dir startup check failed for %s: %s", job_dir, exc)
        degraded.append("job-dir: storage-unhealthy")
    if degraded and os.environ.get("AAP_BRIDGE_ALLOW_DEGRADED_STARTUP", "") != "1":
        set_startup_degraded("; ".join(degraded))
        log.error(
            "storage probes failed; submissions will return 503 until recovery "
            "(%s). Set AAP_BRIDGE_ALLOW_DEGRADED_STARTUP=1 for dev warn-only.",
            "; ".join(degraded),
        )
    else:
        set_startup_degraded(None)
    _host = os.environ.get("AAP_BRIDGE_API_HOST", "127.0.0.1")
    _loopback = ("127.0.0.1", "localhost", "::1")
    if not os.environ.get("AAP_BRIDGE_API_TOKEN", ""):
        if os.environ.get("AAP_BRIDGE_ALLOW_ANON", "") == "1":
            log.warning(
                "AAP_BRIDGE_ALLOW_ANON=1 with no API token: API routes accept "
                "unauthenticated requests. Never use this outside local dev."
            )
        else:
            log.error(
                "AAP_BRIDGE_API_TOKEN is unset: API requests will be rejected "
                "(401). Set the token, or AAP_BRIDGE_ALLOW_ANON=1 for local "
                "dev only."
            )
    elif _host not in _loopback:
        log.warning(
            "API bound to non-loopback host; ensure AAP_BRIDGE_API_TOKEN is set "
            "and the service is behind TLS / firewall."
        )
    yield
    try:
        from aap_migration.api.jobs import manager_or_none

        manager = manager_or_none()
        if manager is not None:
            leftovers = manager.shutdown_drain()
            if leftovers.get("pending") or leftovers.get("running"):
                log.warning(
                    "API shutdown with %d queued and %d running jobs; "
                    "leftovers were marked interrupted (poll the ids for "
                    "the actionable message), resubmit after verifying "
                    "target state",
                    leftovers.get("pending"),
                    leftovers.get("running"),
                )
        else:
            log.warning("API shutdown; no job manager initialized")
    except Exception:
        pass


def _validation_error_detail(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = tuple(err.get("loc", ()))
        # Strip transport locator (body/query/path/params) so automatic 422s
        # share one dialect with hand-raised 422s (e.g. resume from_phase).
        if loc and str(loc[0]) in ("body", "query", "path", "params", "header", "cookie"):
            loc = loc[1:]
        loc_str = ".".join(str(p) for p in loc)
        msg = str(err.get("msg", "invalid"))
        parts.append(f"{loc_str}: {msg}".strip(": ") if loc_str else msg)
    return "; ".join(parts) if parts else "Request validation failed"


def create_app() -> FastAPI:
    """Build the FastAPI application with every v1.x feature as endpoints."""
    try:
        init_api_db()
    except Exception as exc:
        # Degraded startup: lifespan owns the latch and serves reads/health
        # with 503 on submit. Raising here would crash-loop at import on
        # exactly the failure path lifespan was built for.
        log.warning("API DB init in create_app failed (degraded): %s", exc)
    os.environ.setdefault("AAP_BRIDGE_STARTUP_CWD", os.getcwd())
    app = FastAPI(
        title="AAP Bridge REST API",
        description=(
            "REST API for every function/option/feature of the AAP Bridge "
            "release/v1.x CLI (migrate, export, transform, import, IAM, "
            "validate, credentials, reporting, state, prep, cleanup). "
            "Long operations run as background jobs polled via /jobs/{id}. "
            "Set AAP_BRIDGE_API_TOKEN to require X-API-Key on every route."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    prefix = API_V1_PREFIX
    auth = [Depends(require_api_key)]
    # Health/readiness are keyless (orchestrator liveness probes rarely
    # carry API keys; gating them turns every probe into a 401 and restarts
    # a healthy server, churning jobs). version/resources stay authed via
    # per-route dependencies in routers.system; jobs/connections/config
    # stay fully authed here.
    app.include_router(system.router, prefix=prefix)
    app.include_router(jobs.router, prefix=prefix, dependencies=auth)
    app.include_router(connections.router, prefix=prefix, dependencies=auth)
    app.include_router(config.router, prefix=prefix, dependencies=auth)
    # Stack 4 subset: remaining 8 routers (migrations, credentials, iam,
    # validation, analysis, reporting, state, maintenance) land with stack 5.

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Coerce Pydantic's list detail to the single string-detail envelope
        # so clients parse one 422 shape for manual and automatic failures.
        return JSONResponse(status_code=422, content={"detail": _validation_error_detail(exc)})

    def _document_string_422_envelope() -> None:
        """Patch OpenAPI so 422 docs match the string-detail wire shape.

        The validation handler above sends {"detail": str}, but FastAPI's
        default OpenAPI still emits the list-shaped HTTPValidationError.
        Rewrite every 422 response to reference a StringDetailError
        component so codegen clients deserialize what the wire sends.
        """
        original_openapi = app.openapi

        def custom_openapi() -> dict[str, Any]:
            if app.openapi_schema:
                return cast(dict[str, Any], app.openapi_schema)
            schema = cast(dict[str, Any], original_openapi())
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            components["StringDetailError"] = {
                "title": "StringDetailError",
                "type": "object",
                "properties": {"detail": {"title": "Detail", "type": "string"}},
                "required": ["detail"],
            }
            for path_item in schema.get("paths", {}).values():
                if not isinstance(path_item, dict):
                    continue
                for operation in path_item.values():
                    if not isinstance(operation, dict):
                        continue
                    responses = operation.get("responses", {})
                    validation = responses.get("422")
                    if not isinstance(validation, dict):
                        continue
                    content = validation.get("content", {})
                    json_content = content.get("application/json", {})
                    json_content["schema"] = {"$ref": "#/components/schemas/StringDetailError"}
            app.openapi_schema = schema
            return schema

        app.openapi = custom_openapi

    _document_string_422_envelope()

    @app.exception_handler(ValueError)
    async def _value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        # Single error envelope: {"detail": "<message>"} (string detail).
        # ConflictError subclasses ValueError but is handled by routers
        # before reaching here; any stray ConflictError is still a 409.
        from aap_migration.api._errors import ConflictError

        if isinstance(exc, ConflictError):
            return JSONResponse(status_code=409, content={"detail": str(exc)})
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def _key_error_handler(request: Request, exc: KeyError) -> JSONResponse:
        args = getattr(exc, "args", ())
        detail = args[0] if args and isinstance(args[0], str) else str(exc).strip("'\"")
        return JSONResponse(status_code=404, content={"detail": detail})

    @app.exception_handler(RuntimeError)
    async def _runtime_error_handler(request: Request, exc: RuntimeError) -> JSONResponse:
        # Internal invariant violations (unknown job status, queue/storage
        # state) are server faults, never client 400s.
        from aap_migration.api._errors import (
            InternalStatusError,
            ServerShuttingDownError,
            StorageUnhealthyError,
        )

        if isinstance(exc, StorageUnhealthyError | ServerShuttingDownError):
            return JSONResponse(status_code=503, content={"detail": str(exc)})
        if isinstance(exc, InternalStatusError):
            return JSONResponse(status_code=500, content={"detail": str(exc)})
        raise exc

    @app.get("/api/v1/docs", dependencies=auth, include_in_schema=False)
    async def _docs_v1(request: Request) -> HTMLResponse:
        from fastapi.openapi.docs import get_swagger_ui_html

        return get_swagger_ui_html(openapi_url="/api/v1/openapi.json", title="AAP Bridge REST API")

    @app.get("/api/v1/redoc", dependencies=auth, include_in_schema=False)
    async def _redoc_v1(request: Request) -> HTMLResponse:
        from fastapi.openapi.docs import get_redoc_html

        return get_redoc_html(openapi_url="/api/v1/openapi.json", title="AAP Bridge REST API")

    @app.get("/api/v1/openapi.json", dependencies=auth, include_in_schema=False)
    async def _openapi_v1(request: Request) -> JSONResponse:
        return JSONResponse(content=app.openapi())

    # Legacy unversioned discovery paths: redirect to the versioned docs so
    # a future /api/v2 never collides on one docs URL.
    @app.get("/docs", dependencies=auth, include_in_schema=False)
    async def _docs(request: Request) -> RedirectResponse:
        return RedirectResponse(url="/api/v1/docs", status_code=307)

    @app.get("/redoc", dependencies=auth, include_in_schema=False)
    async def _redoc(request: Request) -> RedirectResponse:
        return RedirectResponse(url="/api/v1/redoc", status_code=307)

    @app.get("/openapi.json", dependencies=auth, include_in_schema=False)
    async def _openapi(request: Request) -> RedirectResponse:
        return RedirectResponse(url="/api/v1/openapi.json", status_code=307)

    return app


app = create_app()


def main() -> None:
    """Run the API with uvicorn (``aap-bridge-api`` entry point)."""
    import uvicorn

    host = os.environ.get("AAP_BRIDGE_API_HOST", "127.0.0.1")
    cert = os.environ.get("AAP_BRIDGE_API_TLS_CERT", "")
    key = os.environ.get("AAP_BRIDGE_API_TLS_KEY", "")
    kwargs: dict[str, str] = {}
    if cert and key:
        kwargs["ssl_certfile"] = cert
        kwargs["ssl_keyfile"] = key
    if (
        host not in ("127.0.0.1", "localhost", "::1")
        and not (cert and key)
        and not os.environ.get("AAP_BRIDGE_API_TOKEN", "")
    ):
        log.error(
            "Non-loopback bind without TLS cert/key or API token: "
            "set AAP_BRIDGE_API_TLS_CERT/KEY and AAP_BRIDGE_API_TOKEN."
        )
    uvicorn.run(
        "aap_migration.api.app:app",
        host=host,
        port=int(os.environ.get("AAP_BRIDGE_API_PORT", "8000")),
        reload=False,
        **kwargs,
    )


if __name__ == "__main__":
    main()
