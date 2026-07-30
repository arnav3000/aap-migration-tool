"""Unit tests for IAM data models, exceptions, and report helpers."""

from __future__ import annotations

from pathlib import Path

from aap_migration.iam.exceptions import AuthenticationError, PaginationError
from aap_migration.iam.models import (
    CrossOrgShare,
    IAMAuditResult,
    IAMCheckpoint,
    MigrationStats,
    OrgSummary,
    PermissionEntry,
    SystemRoleEntry,
    TeamMembership,
)
from aap_migration.iam.report import (
    export_iam_json,
    generate_iam_html_report,
    load_audit_result_from_json,
    write_iam_report,
)


def test_cross_org_share_to_dict() -> None:
    share = CrossOrgShare(
        resource_type="projects",
        resource_name="Shared",
        resource_org="Default",
        shared_with_orgs=["Other"],
        permission_count=3,
    )
    assert share.to_dict()["permission_count"] == 3
    assert share.to_dict()["shared_with_orgs"] == ["Other"]


def test_permission_entry_round_trip_and_dedup_key() -> None:
    raw = {
        "resource_type": "projects",
        "resource_id": 7,
        "resource_name": "Demo",
        "resource_org": "Default",
        "role_name": "admin",
        "principal_type": "user",
        "principal_id": 3,
        "principal_name": "alice",
        "principal_org": "Default",
        "is_cross_org": True,
        "status": "migrated",
        "error": "",
    }
    entry = PermissionEntry.from_dict(raw)
    assert entry.dedup_key == ("projects", 7, "admin", "user", 3)
    assert entry.to_dict() == raw


def test_team_membership_and_system_role_to_dict() -> None:
    membership = TeamMembership(
        team_id=1,
        team_name="Ops",
        team_org="Default",
        user_id=2,
        username="bob",
        status="failed",
        error="denied",
    )
    assert membership.to_dict()["username"] == "bob"

    role = SystemRoleEntry(user_id=1, username="root", flag="is_superuser")
    assert role.to_dict() == {
        "user_id": 1,
        "username": "root",
        "flag": "is_superuser",
    }


def test_org_summary_success_rate_and_migration_stats() -> None:
    empty = OrgSummary(org_name="Default")
    assert empty.success_rate == 0.0

    summary = OrgSummary(
        org_name="Default",
        permissions_total=4,
        permissions_migrated=3,
        permissions_failed=1,
    )
    assert summary.success_rate == 75.0
    dumped = summary.to_dict()
    assert dumped["success_rate"] == 75.0

    stats = MigrationStats(permissions_found=10, permissions_migrated=8)
    assert stats.to_dict()["permissions_found"] == 10


def test_iam_audit_result_and_checkpoint_round_trip() -> None:
    permission = PermissionEntry(
        resource_type="projects",
        resource_id=1,
        resource_name="App",
        resource_org="Default",
        role_name="use",
        principal_type="team",
        principal_id=5,
        principal_name="Dev",
        principal_org="Default",
    )
    result = IAMAuditResult(
        mode="audit",
        source_url="https://aap.example.com/api/controller/v2/",
        permissions=[permission],
        team_memberships=[
            TeamMembership(1, "Dev", "Default", 2, "alice"),
        ],
        system_roles=[SystemRoleEntry(1, "alice", "is_system_auditor")],
        cross_org_shares=[
            CrossOrgShare("projects", "Shared", "Default", ["Other"], 2),
        ],
        org_summaries={"Default": OrgSummary(org_name="Default", permissions_total=1)},
        stats=MigrationStats(permissions_found=1),
    )
    payload = result.to_dict()
    assert payload["metadata"]["mode"] == "audit"
    assert len(payload["permissions"]) == 1

    checkpoint = IAMCheckpoint.from_dict(
        {
            "version": 2,
            "scan_strategy": "user",
            "source_url": "https://aap.example.com",
            "permissions": [{"resource_type": "teams"}],
            "permissions_found": 5,
        }
    )
    assert checkpoint.version == 2
    assert checkpoint.scan_strategy == "user"
    assert checkpoint.to_dict()["permissions_found"] == 5


def test_iam_exceptions_include_context() -> None:
    auth_err = AuthenticationError("/users/", 403, entries_succeeded=2, entries_remaining=5)
    assert auth_err.status_code == 403
    assert "2 entries succeeded" in str(auth_err)

    page_err = PaginationError(
        "teams/",
        "bad page",
        url="https://aap.example.com/teams/?page=2",
        status_code=500,
        items_collected=10,
        expected_count=20,
    )
    assert page_err.items_collected == 10
    assert "server reported 20" in str(page_err)


def test_iam_report_json_and_html_export(tmp_path: Path) -> None:
    result = IAMAuditResult(
        mode="dry_run",
        source_url="https://aap.example.com/api/controller/v2/",
        permissions=[
            PermissionEntry(
                resource_type="projects",
                resource_id=1,
                resource_name="Demo",
                resource_org="Default",
                role_name="admin",
                principal_type="user",
                principal_id=1,
                principal_name="alice",
                principal_org="Default",
                status="pending",
            )
        ],
        stats=MigrationStats(permissions_found=1),
    )

    json_path = tmp_path / "iam.json"
    export_iam_json(result, str(json_path))
    loaded = load_audit_result_from_json(str(json_path))
    assert loaded.mode == "dry_run"
    assert len(loaded.permissions) == 1
    assert loaded.permissions[0].resource_name == "Demo"

    html = generate_iam_html_report(result)
    assert "Dry-Run" in html or "dry_run" in html.lower()
    assert "aap.example.com" in html

    html_path = tmp_path / "reports"
    write_iam_report(result, str(html_path))
    assert (html_path / "iam_report.html").read_text()
    assert (html_path / "iam_report.json").exists()
