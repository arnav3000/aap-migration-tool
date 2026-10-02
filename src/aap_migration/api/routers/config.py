"""Config endpoints (mirrors ``config validate`` / ``config show``)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from aap_migration.api.context import build_ephemeral_context, close_job_context
from aap_migration.api.schemas import (
    ConfigShowRequest,
    ConfigValidateOut,
    ConfigValidateRequest,
)
from aap_migration.api.security import redact_backend_error
from aap_migration.config import MigrationConfig

log = logging.getLogger("aap_migration.api.config")

router = APIRouter(tags=["config"])


def _config_summary(config: MigrationConfig) -> dict:
    import os

    return {
        "source_url": config.source.url,
        "target_url": config.target.url,
        "source_verify_ssl": config.source.verify_ssl,
        "target_verify_ssl": config.target.verify_ssl,
        # Basename only: absolute server-local DB paths must not leak (#30).
        "state_db_path": os.path.basename(str(config.state.db_path)),
        "default_batch_size": config.performance.batch_sizes.get("default"),
        "host_batch_size": config.performance.batch_sizes.get("hosts"),
        "max_concurrent": config.performance.max_concurrent,
        "rate_limit": config.performance.rate_limit,
    }


@router.post("/config/validate", response_model=ConfigValidateOut)
def validate_config(body: ConfigValidateRequest) -> dict:
    """Validate active connection config, optionally testing connectivity.

    ``valid`` is a constant-True success marker on 200 (pinned as
    Literal[True] in OpenAPI; real failures come back as 400/502, never
    ``valid: false``): use the status code, not the boolean, for failure
    detection. ``connectivity`` is always present (empty ``{}`` unless
    ``check_connectivity`` was requested).
    """
    try:
        ctx = build_ephemeral_context(body.source_id, body.target_id)
        config = ctx.config
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        # Settings checks mirror _validate_settings in cli/commands/config.py
        for resource_type, batch_size in config.performance.batch_sizes.items():
            if batch_size <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Batch size must be positive: {resource_type}={batch_size}",
                )
        if config.performance.max_concurrent <= 0:
            raise HTTPException(status_code=400, detail="Max concurrent requests must be positive")
        if config.performance.rate_limit <= 0:
            raise HTTPException(status_code=400, detail="Rate limit must be positive")

        connectivity: dict = {}
        if body.check_connectivity:
            from aap_migration.utils.ssrf import reverify_execution_url_bounded

            try:
                reverify_execution_url_bounded(str(config.source.url))
                reverify_execution_url_bounded(str(config.target.url))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            async def _main() -> None:
                await ctx.source_client.get("ping/")
                await ctx.target_client.get("ping/")

            async def _probe() -> None:
                await asyncio.wait_for(_main(), timeout=30)

            try:
                asyncio.run(_probe())
                connectivity = {"source": "reachable", "target": "reachable"}
            except TimeoutError as exc:
                log.exception("config connectivity check timed out")
                raise HTTPException(status_code=502, detail=redact_backend_error(exc)) from exc
            except Exception as exc:
                log.exception("config connectivity check failed")
                raise HTTPException(status_code=502, detail=redact_backend_error(exc)) from exc

        return {
            "valid": True,
            "summary": _config_summary(config),
            "connectivity": connectivity,
        }
    finally:
        close_job_context(ctx)


@router.post("/config/show")
def show_config(body: ConfigShowRequest) -> dict:
    """Display active configuration with tokens masked (mirrors ``config show``)."""
    try:
        ctx = build_ephemeral_context(body.source_id, body.target_id)
        config = ctx.config
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        summary = _config_summary(config)
    finally:
        close_job_context(ctx)
    summary.update(
        {
            "source_token": "*" * 40 + " (masked)",
            "target_token": "*" * 40 + " (masked)",
            "batch_sizes": dict(config.performance.batch_sizes),
        }
    )
    return summary
