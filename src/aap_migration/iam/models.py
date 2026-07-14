"""Data models for IAM analysis and migration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PermissionEntry:
    """A single role assignment on a resource."""

    resource_type: str
    resource_id: int
    resource_name: str
    resource_org: str
    role_name: str
    principal_type: str  # "user" or "team"
    principal_id: int
    principal_name: str
    principal_org: str
    is_cross_org: bool = False
    status: str = "pending"  # pending, migrated, failed, skipped
    error: str = ""

    @property
    def dedup_key(self) -> tuple:
        return (
            self.resource_type,
            self.resource_id,
            self.role_name,
            self.principal_type,
            self.principal_id,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PermissionEntry":
        return cls(
            resource_type=data["resource_type"],
            resource_id=data["resource_id"],
            resource_name=data["resource_name"],
            resource_org=data["resource_org"],
            role_name=data["role_name"],
            principal_type=data["principal_type"],
            principal_id=data["principal_id"],
            principal_name=data["principal_name"],
            principal_org=data["principal_org"],
            is_cross_org=data.get("is_cross_org", False),
            status=data.get("status", "pending"),
            error=data.get("error", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "resource_name": self.resource_name,
            "resource_org": self.resource_org,
            "role_name": self.role_name,
            "principal_type": self.principal_type,
            "principal_id": self.principal_id,
            "principal_name": self.principal_name,
            "principal_org": self.principal_org,
            "is_cross_org": self.is_cross_org,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class TeamMembership:
    """A user's membership in a team."""

    team_id: int
    team_name: str
    team_org: str
    user_id: int
    username: str
    status: str = "pending"
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "team_name": self.team_name,
            "team_org": self.team_org,
            "user_id": self.user_id,
            "username": self.username,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class SystemRoleEntry:
    """A user's system-level flag (superuser / system auditor)."""

    user_id: int
    username: str
    flag: str  # "is_superuser" or "is_system_auditor"

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "flag": self.flag,
        }


@dataclass
class CrossOrgShare:
    """A resource shared across organization boundaries."""

    resource_type: str
    resource_name: str
    resource_org: str
    shared_with_orgs: list[str] = field(default_factory=list)
    permission_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "resource_name": self.resource_name,
            "resource_org": self.resource_org,
            "shared_with_orgs": self.shared_with_orgs,
            "permission_count": self.permission_count,
        }


@dataclass
class OrgSummary:
    """Aggregated permission stats for a single organization."""

    org_name: str
    resources_scanned: int = 0
    permissions_total: int = 0
    permissions_by_type: dict[str, int] = field(default_factory=dict)
    permissions_by_role: dict[str, int] = field(default_factory=dict)
    permissions_migrated: int = 0
    permissions_failed: int = 0
    permissions_skipped: int = 0
    team_memberships_total: int = 0
    team_memberships_migrated: int = 0
    team_memberships_failed: int = 0
    cross_org_shares: int = 0

    @property
    def success_rate(self) -> float:
        if self.permissions_total == 0:
            return 0.0
        return (self.permissions_migrated / self.permissions_total) * 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "org_name": self.org_name,
            "resources_scanned": self.resources_scanned,
            "permissions_total": self.permissions_total,
            "permissions_by_type": self.permissions_by_type,
            "permissions_by_role": self.permissions_by_role,
            "permissions_migrated": self.permissions_migrated,
            "permissions_failed": self.permissions_failed,
            "permissions_skipped": self.permissions_skipped,
            "team_memberships_total": self.team_memberships_total,
            "team_memberships_migrated": self.team_memberships_migrated,
            "team_memberships_failed": self.team_memberships_failed,
            "cross_org_shares": self.cross_org_shares,
            "success_rate": round(self.success_rate, 1),
        }


@dataclass
class MigrationStats:
    """Overall migration statistics."""

    resources_scanned: int = 0
    permissions_found: int = 0
    permissions_migrated: int = 0
    permissions_failed: int = 0
    permissions_skipped: int = 0
    permissions_deduplicated: int = 0
    user_permissions_total: int = 0
    user_permissions_pending: int = 0
    team_permissions_total: int = 0
    team_memberships_found: int = 0
    team_memberships_migrated: int = 0
    team_memberships_failed: int = 0
    team_memberships_skipped: int = 0
    system_roles_found: int = 0
    cross_org_shares: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "resources_scanned": self.resources_scanned,
            "permissions_found": self.permissions_found,
            "permissions_migrated": self.permissions_migrated,
            "permissions_failed": self.permissions_failed,
            "permissions_skipped": self.permissions_skipped,
            "permissions_deduplicated": self.permissions_deduplicated,
            "user_permissions_total": self.user_permissions_total,
            "user_permissions_pending": self.user_permissions_pending,
            "team_permissions_total": self.team_permissions_total,
            "team_memberships_found": self.team_memberships_found,
            "team_memberships_migrated": self.team_memberships_migrated,
            "team_memberships_failed": self.team_memberships_failed,
            "team_memberships_skipped": self.team_memberships_skipped,
            "system_roles_found": self.system_roles_found,
            "cross_org_shares": self.cross_org_shares,
        }


@dataclass
class IAMAuditResult:
    """Complete result of an IAM audit or migration run."""

    mode: str  # "audit", "migrate", or "dry_run"
    source_url: str
    permissions: list[PermissionEntry] = field(default_factory=list)
    team_memberships: list[TeamMembership] = field(default_factory=list)
    system_roles: list[SystemRoleEntry] = field(default_factory=list)
    cross_org_shares: list[CrossOrgShare] = field(default_factory=list)
    org_summaries: dict[str, OrgSummary] = field(default_factory=dict)
    stats: MigrationStats = field(default_factory=MigrationStats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": {
                "mode": self.mode,
                "source_url": self.source_url,
            },
            "statistics": self.stats.to_dict(),
            "org_summaries": {
                k: v.to_dict() for k, v in self.org_summaries.items()
            },
            "permissions": [p.to_dict() for p in self.permissions],
            "team_memberships": [m.to_dict() for m in self.team_memberships],
            "system_roles": [r.to_dict() for r in self.system_roles],
            "cross_org_shares": [c.to_dict() for c in self.cross_org_shares],
        }


@dataclass
class IAMCheckpoint:
    """Persisted state for resumable IAM scans."""

    version: int = 1
    scan_strategy: str = "resource"
    source_url: str = ""
    started_at: str = ""
    updated_at: str = ""
    completed_resource_types: list[str] = field(default_factory=list)
    completed_user_ids: list[int] = field(default_factory=list)
    completed_team_ids: list[int] = field(default_factory=list)
    permissions: list[dict[str, Any]] = field(default_factory=list)
    resources_scanned: int = 0
    permissions_found: int = 0
    permissions_deduplicated: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "scan_strategy": self.scan_strategy,
            "source_url": self.source_url,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_resource_types": self.completed_resource_types,
            "completed_user_ids": self.completed_user_ids,
            "completed_team_ids": self.completed_team_ids,
            "permissions": self.permissions,
            "resources_scanned": self.resources_scanned,
            "permissions_found": self.permissions_found,
            "permissions_deduplicated": self.permissions_deduplicated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IAMCheckpoint":
        return cls(
            version=data.get("version", 1),
            scan_strategy=data.get("scan_strategy", "resource"),
            source_url=data.get("source_url", ""),
            started_at=data.get("started_at", ""),
            updated_at=data.get("updated_at", ""),
            completed_resource_types=data.get(
                "completed_resource_types", []
            ),
            completed_user_ids=data.get("completed_user_ids", []),
            completed_team_ids=data.get("completed_team_ids", []),
            permissions=data.get("permissions", []),
            resources_scanned=data.get("resources_scanned", 0),
            permissions_found=data.get("permissions_found", 0),
            permissions_deduplicated=data.get(
                "permissions_deduplicated", 0
            ),
        )
