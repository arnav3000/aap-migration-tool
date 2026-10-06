"""Connection request/response schemas (part of api.schemas package)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# -- connections ---------------------------------------------------------
class ConnectionCreate(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    kind: Literal["source", "target"]
    url: str
    token: str
    verify_ssl: bool = True
    timeout: int = Field(default=30, ge=1, le=1200)


class ConnectionUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = None
    url: str | None = None
    token: str | None = None
    verify_ssl: bool | None = None
    timeout: int | None = Field(default=None, ge=1, le=1200)


class ConnectionReplace(BaseModel):
    """Full-replace body for PUT (all fields required; omitted fields reset)."""

    model_config = {"extra": "forbid"}

    name: str
    url: str
    token: str
    verify_ssl: bool = True
    timeout: int = Field(default=30, ge=1, le=1200)


class ConnectionOut(BaseModel):
    """Public connection view. The secret token is never serialized."""

    id: str
    name: str
    kind: str
    url: str
    verify_ssl: bool
    timeout: int
    created_at: str | None = None
    updated_at: str | None = None


class ActiveConfigIn(BaseModel):
    model_config = {"extra": "forbid"}

    source_id: str | None = None
    target_id: str | None = None
    clear_source: bool = Field(
        default=False,
        description="Set true to explicitly detach the active source (None keeps the current value)",
    )
    clear_target: bool = Field(
        default=False,
        description="Set true to explicitly detach the active target (None keeps the current value)",
    )


class ActiveConfigOut(BaseModel):
    source_id: str | None = None
    target_id: str | None = None


class ConnectionListOut(BaseModel):
    """GET /connections envelope (true pre-page total)."""

    model_config = {"extra": "forbid"}

    items: list[ConnectionOut] = Field(default_factory=list)
    total: int = 0
    limit: int = 100
    offset: int = 0
