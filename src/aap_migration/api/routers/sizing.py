"""Sizing calculator endpoints — stubbed until sizing code is ported."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("/sizing/calculate", response_model=None)
def calculate_sizing(params: dict[str, Any]) -> None:
    raise HTTPException(
        status_code=501,
        detail="Sizing calculator not yet available. This feature will be added in a future release.",
    )


@router.post("/sizing/dynamic", response_model=None)
def calculate_dynamic_sizing(params: dict[str, Any]) -> None:
    raise HTTPException(
        status_code=501,
        detail="Dynamic sizing not yet available. This feature will be added in a future release.",
    )
