"""SQLAlchemy models for the API layer (connections, job tracking, migration plans)."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from aap_migration.migration.models import Base


class Connection(Base):
    """Stored AAP connection for use by the UI."""

    __tablename__ = "api_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    type: Mapped[str] = mapped_column(String(20), nullable=False, default="awx")
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="source")
    verify_ssl: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timeout: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    ping_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    auth_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Connection(id={self.id}, name='{self.name}', role='{self.role}')>"


class JobRecord(Base):
    """Persisted job record — survives container restarts."""

    __tablename__ = "api_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    seq_id: Mapped[int] = mapped_column(Integer, autoincrement=True, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<JobRecord(id={self.id}, seq_id={self.seq_id}, type='{self.type}', status='{self.status}')>"


class MigrationPlan(Base):
    """A saved multi-source, phased migration plan."""

    __tablename__ = "api_migration_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    destination_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_connections.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MigrationPlanSource(Base):
    """A source connection within a migration plan."""

    __tablename__ = "api_migration_plan_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_migration_plans.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_connections.id", ondelete="CASCADE"), nullable=False
    )
    name_prefix: Mapped[str | None] = mapped_column(String(100), nullable=True)
    analysis_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_jobs.id", ondelete="SET NULL"), nullable=True
    )


class MigrationPlanPhase(Base):
    """A phase within a migration plan."""

    __tablename__ = "api_migration_plan_phases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_migration_plans.id", ondelete="CASCADE"), nullable=False
    )
    phase_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_jobs.id", ondelete="SET NULL"), nullable=True
    )


class MigrationPlanPhaseOrg(Base):
    """An organization assigned to a phase."""

    __tablename__ = "api_migration_plan_phase_orgs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    phase_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_migration_plan_phases.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_migration_plan_sources.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[int] = mapped_column(Integer, nullable=False)
    org_name: Mapped[str] = mapped_column(String(255), nullable=False)
