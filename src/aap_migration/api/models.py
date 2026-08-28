"""SQLAlchemy models for the API layer (isolated from migration tables)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ApiBase(DeclarativeBase):
    """Separate declarative base so api tables don't interfere with migration Base."""

    pass


class Connection(ApiBase):
    """Stored AAP connection for use by the API/UI."""

    __tablename__ = "api_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    # encrypted token (or plaintext legacy)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    # source | target
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="source")
    verify_ssl: Mapped[bool] = mapped_column(default=True)
    timeout: Mapped[int] = mapped_column(Integer, default=30)
    ping_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    auth_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Connection(id={self.id}, name='{self.name}', role='{self.role}')>"


class JobRecord(ApiBase):
    """Persisted job record that survives container restarts."""

    __tablename__ = "api_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    seq_id: Mapped[int] = mapped_column(Integer, unique=True, autoincrement=False, nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False, default="migration")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<JobRecord(id={self.id}, seq_id={self.seq_id}, type='{self.type}', status='{self.status}')>"


class MigrationPlan(ApiBase):
    """Persisted migration plan for Task 4 planner (isolated)."""

    __tablename__ = "api_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")
    phases_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<MigrationPlan(id={self.id}, name='{self.name}', status='{self.status}')>"


class ApiSetting(ApiBase):
    """Simple key/value store for API settings (e.g. concurrency)."""

    __tablename__ = "api_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<ApiSetting(key={self.key}, value={self.value})>"


# Backwards-compat alias — older pyc expects `Job`
Job = JobRecord
