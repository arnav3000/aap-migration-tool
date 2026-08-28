"""Sizing API (Task 5 clean) — calculate / dynamic."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, verify_api_token
from aap_migration.api.schemas import SizingCalculateRequest, SizingDynamicRequest, SizingResponse
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.resources import RESOURCE_REGISTRY, get_exportable_types

router = APIRouter(prefix="/sizing", tags=["sizing"], dependencies=[Depends(verify_api_token)])


async def _counts_for_connection(
    conn, resource_types: list[str] | None, append_log=None
) -> dict[str, int]:
    # Reuse exporter counting logic lightweight
    from aap_migration.api.crypto import decrypt_token
    from aap_migration.client.aap_source_client import AAPSourceClient
    from aap_migration.config import AAPInstanceConfig

    try:
        token = decrypt_token(conn.token)
    except Exception:
        token = conn.token
    cfg = AAPInstanceConfig(
        url=conn.url, token=token, verify_ssl=conn.verify_ssl, timeout=conn.timeout
    )
    client = AAPSourceClient(config=cfg)
    if resource_types is None:
        rtypes = get_exportable_types()
    else:
        rtypes = resource_types
    counts: dict[str, int] = {}
    for rt in rtypes:
        if rt not in RESOURCE_REGISTRY:
            continue
        try:
            resp = await client.get(f"{rt}/", params={"page_size": 1})
            counts[rt] = resp.get("count", 0)
        except Exception as e:
            if append_log:
                append_log(f"{rt} count failed: {e}")
            counts[rt] = 0
    try:
        await client.close()
    except Exception:
        pass
    return counts


@router.post("/calculate", response_model=SizingResponse)
async def sizing_calculate(
    body: SizingCalculateRequest, db: Session = Depends(get_db)
) -> SizingResponse:
    conn = ConnectionService.get(db, body.connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    if body.resource_types:
        for rt in body.resource_types:
            if rt not in RESOURCE_REGISTRY:
                raise HTTPException(status_code=422, detail=f"Unknown resource type: {rt}")
    counts = await _counts_for_connection(conn, body.resource_types)
    total = sum(counts.values())
    # Simple sizing: batch 200, duration estimate 0.5s per 100 resources + 10s overhead
    recommended_batch = 200 if total > 1000 else 100
    estimated = int(total * 0.005 + 10) if total else 10
    rtypes = body.resource_types or get_exportable_types()
    return SizingResponse(
        connection_id=body.connection_id,
        resource_types=rtypes,
        total_resources=total,
        counts=counts,
        recommended_batch_size=recommended_batch,
        estimated_duration_seconds=estimated,
    )


@router.post("/dynamic", response_model=SizingResponse)
async def sizing_dynamic(
    body: SizingDynamicRequest, db: Session = Depends(get_db)
) -> SizingResponse:
    conn = ConnectionService.get(db, body.connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    if body.resource_types:
        for rt in body.resource_types:
            if rt not in RESOURCE_REGISTRY:
                raise HTTPException(status_code=422, detail=f"Unknown resource type: {rt}")
    counts = await _counts_for_connection(conn, body.resource_types)
    total = sum(counts.values())
    # Dynamic sizing adjusts batch based on sample_size hint
    sample = body.sample_size or total or 1
    # Example: if sample large, reduce batch to avoid memory pressure
    if sample > 5000:
        recommended = 100
    elif sample > 1000:
        recommended = 150
    else:
        recommended = 200
    estimated = int(total * 0.005 + 5)
    rtypes = body.resource_types or get_exportable_types()
    return SizingResponse(
        connection_id=body.connection_id,
        resource_types=rtypes,
        total_resources=total,
        counts=counts,
        recommended_batch_size=recommended,
        estimated_duration_seconds=estimated,
    )
