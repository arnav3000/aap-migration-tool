"""Health and version endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from aap_migration import __version__
from aap_migration.api.dependencies import verify_api_token
from aap_migration.api.schemas import HealthResponse, VersionResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__, api_prefix="/api")


@router.get("/version", response_model=VersionResponse)
async def version() -> VersionResponse:
    return VersionResponse(version=__version__)


# Protected variant for verifying auth works
@router.get(
    "/health/protected", response_model=HealthResponse, dependencies=[Depends(verify_api_token)]
)
async def health_protected() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__, api_prefix="/api")
