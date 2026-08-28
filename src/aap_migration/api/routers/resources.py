"""Resource registry endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from aap_migration.api.schemas import ResourcesListResponse, ResourceTypeInfoResponse
from aap_migration.resources import RESOURCE_REGISTRY

router = APIRouter(prefix="/resources", tags=["resources"])


@router.get("", response_model=ResourcesListResponse)
async def list_resources() -> ResourcesListResponse:
    resources = [
        ResourceTypeInfoResponse(
            name=info.name,
            endpoint=info.endpoint,
            description=info.description,
            migration_order=info.migration_order,
            cleanup_order=info.cleanup_order,
            has_exporter=info.has_exporter,
            has_importer=info.has_importer,
            has_transformer=info.has_transformer,
            batch_size=info.batch_size,
            use_bulk_api=info.use_bulk_api,
        )
        for info in sorted(RESOURCE_REGISTRY.values(), key=lambda x: x.migration_order)
    ]
    return ResourcesListResponse(count=len(resources), resources=resources)


@router.get("/{resource_type}", response_model=ResourceTypeInfoResponse)
async def get_resource(resource_type: str) -> ResourceTypeInfoResponse:
    info = RESOURCE_REGISTRY.get(resource_type)
    if not info:
        raise HTTPException(status_code=404, detail=f"Unknown resource type: {resource_type}")
    return ResourceTypeInfoResponse(
        name=info.name,
        endpoint=info.endpoint,
        description=info.description,
        migration_order=info.migration_order,
        cleanup_order=info.cleanup_order,
        has_exporter=info.has_exporter,
        has_importer=info.has_importer,
        has_transformer=info.has_transformer,
        batch_size=info.batch_size,
        use_bulk_api=info.use_bulk_api,
    )
