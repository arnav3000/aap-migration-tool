"""Settings API (Task 5 clean) — concurrency."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db, verify_api_token
from aap_migration.api.models import ApiSetting
from aap_migration.api.schemas import SettingsConcurrencyResponse, SettingsConcurrencyUpdate

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(verify_api_token)])

DEFAULTS = {"max_concurrent": 5, "batch_size": 200, "rate_limit": 20}


def _get_settings(db: Session) -> dict[str, Any]:
    settings = dict(DEFAULTS)
    rows = db.query(ApiSetting).all()
    for r in rows:
        try:
            # Try to parse int
            if r.key in ("max_concurrent", "batch_size", "rate_limit"):
                settings[r.key] = int(r.value)
            else:
                settings[r.key] = r.value
        except Exception:
            settings[r.key] = r.value
    return settings


@router.get("/concurrency", response_model=SettingsConcurrencyResponse)
async def get_concurrency(db: Session = Depends(get_db)) -> SettingsConcurrencyResponse:
    s = _get_settings(db)
    return SettingsConcurrencyResponse(
        max_concurrent=int(s.get("max_concurrent", DEFAULTS["max_concurrent"])),
        batch_size=int(s.get("batch_size", DEFAULTS["batch_size"])),
        rate_limit=int(s.get("rate_limit", DEFAULTS["rate_limit"])),
    )


@router.put("/concurrency", response_model=SettingsConcurrencyResponse)
async def put_concurrency(
    body: SettingsConcurrencyUpdate, db: Session = Depends(get_db)
) -> SettingsConcurrencyResponse:
    # Update only provided fields
    updates = body.model_dump(exclude_unset=True)
    for k, v in updates.items():
        if v is None:
            continue
        existing = db.get(ApiSetting, k)
        if existing:
            existing.value = str(v)
        else:
            db.add(ApiSetting(key=k, value=str(v)))
    db.commit()
    s = _get_settings(db)
    return SettingsConcurrencyResponse(
        max_concurrent=int(s.get("max_concurrent", DEFAULTS["max_concurrent"])),
        batch_size=int(s.get("batch_size", DEFAULTS["batch_size"])),
        rate_limit=int(s.get("rate_limit", DEFAULTS["rate_limit"])),
    )


@router.get("", response_model=dict)
async def get_all_settings(db: Session = Depends(get_db)) -> dict:
    return _get_settings(db)
