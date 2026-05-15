"""Resource browsing endpoints."""

from __future__ import annotations

import asyncio
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

    async def _fetch_types() -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        client = ConnectionService.build_source_client(conn)
        async with client:
            for rtype, info in sorted(
                RESOURCE_REGISTRY.items(), key=lambda x: x[1].migration_order
            ):
                try:
                    resp = await client.get(info.endpoint, params={"page_size": 1, "page": 1})
                    count = resp.get("count", 0)
                except Exception:
                    count = 0
                items.append(
                    {
                        "name": rtype,
                        "label": info.description or rtype.replace("_", " ").title(),
                        "api_path": info.endpoint,
                        "count": count,
                    }
                )
        return items

    try:
        result = await asyncio.wait_for(_fetch_types(), timeout=15.0)
    except (asyncio.TimeoutError, Exception):
        result = [
            {
                "name": rtype,
                "label": info.description or rtype.replace("_", " ").title(),
                "api_path": info.endpoint,
                "count": -1,
            }
            for rtype, info in sorted(
                RESOURCE_REGISTRY.items(), key=lambda x: x[1].migration_order
            )
        ]

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
