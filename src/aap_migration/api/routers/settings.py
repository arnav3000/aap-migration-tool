"""Application settings endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db
from aap_migration.api.models import AppSetting
from aap_migration.api.schemas import ConcurrencySettingResponse, ConcurrencySettingUpdate

router = APIRouter()

DEFAULT_MAX_CONCURRENT = 15


@router.get("/settings/concurrency", response_model=ConcurrencySettingResponse)
def get_concurrency(db: Session = Depends(get_db)) -> ConcurrencySettingResponse:
    row = db.query(AppSetting).filter(AppSetting.key == "max_concurrent").first()
    value = int(row.value) if row else DEFAULT_MAX_CONCURRENT
    return ConcurrencySettingResponse(max_concurrent=value)


@router.put("/settings/concurrency", response_model=ConcurrencySettingResponse)
def update_concurrency(
    body: ConcurrencySettingUpdate, db: Session = Depends(get_db)
) -> ConcurrencySettingResponse:
    row = db.query(AppSetting).filter(AppSetting.key == "max_concurrent").first()
    if row:
        row.value = str(body.max_concurrent)
    else:
        db.add(AppSetting(key="max_concurrent", value=str(body.max_concurrent)))
    db.commit()
    return ConcurrencySettingResponse(max_concurrent=body.max_concurrent)
