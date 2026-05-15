"""Resource browsing endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.resources import RESOURCE_REGISTRY

router = APIRouter()


@router.get("/connections/{conn_id}/resources")
async def list_resource_types(conn_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    conn = ConnectionService.get(db, conn_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    client = ConnectionService.build_source_client(conn)
    result: list[dict[str, Any]] = []
    try:
        async with client:
            for rtype, info in sorted(
                RESOURCE_REGISTRY.items(), key=lambda x: x[1].migration_order
            ):
                try:
                    data = await client.get_paginated(info.endpoint, page_size=1)
                    count = len(data) if data else 0
                    if hasattr(client, "_last_count"):
                        count = client._last_count
                except Exception:
                    count = 0
                result.append(
                    {
                        "type": rtype,
                        "endpoint": info.endpoint,
                        "description": info.description,
                        "count": count,
                    }
                )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to query AAP: {exc}") from None

    return result


@router.get("/connections/{conn_id}/resources/{resource_type}")
async def list_resources(
    conn_id: str,
    resource_type: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    conn = ConnectionService.get(db, conn_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    info = RESOURCE_REGISTRY.get(resource_type)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Unknown resource type: {resource_type}")

    client = ConnectionService.build_source_client(conn)
    try:
        async with client:
            resources = await client.get_paginated(info.endpoint, page_size=page_size)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to query AAP: {exc}") from None

    return resources
