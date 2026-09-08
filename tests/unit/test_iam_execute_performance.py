"""Automated tests for IAM execute phase — performance fix and correctness.

Coverage:
  1. No sleep during _migrate_permissions or _migrate_team_memberships
  2. Transport layer retries 429 (config + behaviour)
  3. Stats accuracy — migrated/failed counts are correct
  4. Resume — already-processed entries are skipped, not re-POSTed
  5. Dry-run — no POSTs issued, entries marked dry_run
  6. Edge cases — resource not found, principal not found, role not found
  7. Team membership correctness
"""

import json
from unittest.mock import patch, MagicMock

import pytest
import requests

from aap_migration.iam.exceptions import AuthenticationError
from aap_migration.iam.models import MigrationStats, PermissionEntry, TeamMembership


# ── Helpers ───────────────────────────────────────────────────────────────────


def _real_response(status_code, body=None):
    resp = requests.Response()
    resp.status_code = status_code
    resp._content = json.dumps(body or {}).encode()
    resp.headers["Content-Type"] = "application/json"
    resp.url = "https://target.example.com/api/controller/v2/roles/99/users/"
    return resp


def _make_analyser(**kwargs):
    with patch("requests.Session"):
        from aap_migration.iam.analyser import IAMAnalyser
        analyser = IAMAnalyser(
            source_url="https://example.com/api/v2",
            source_token="fake-token",
            target_url="https://target.com/api/controller/v2",
            target_token="fake-target-token",
            verify_ssl=False,
            **kwargs,
        )
    return analyser


def _make_entries(count, status=None):
    entries = [
        PermissionEntry(
            resource_type="credentials",
            resource_id=i,
            resource_name=f"cred-{i}",
            resource_org="TestOrg",
            role_name="Admin",
            principal_type="user",
            principal_id=i,
            principal_name=f"user-{i}",
            principal_org="TestOrg",
        )
        for i in range(1, count + 1)
    ]
    if status:
        for e in entries:
            e.status = status
    return entries


def _make_memberships(count, status=None):
    memberships = [
        TeamMembership(
            team_id=1,
            team_name="team-1",
            team_org="TestOrg",
            user_id=i,
            username=f"user-{i}",
        )
        for i in range(1, count + 1)
    ]
    if status:
        for m in memberships:
            m.status = status
    return memberships


def _setup_analyser_for_permissions(analyser, post_response=None):
    """Wire up ID resolution and role cache stubs."""
    analyser._get_target_id = lambda rt, sid: sid
    analyser._target_paginate = lambda ep: [{"name": "Admin", "id": 100}]
    if post_response is not None:
        analyser._target_post = lambda ep, data: post_response


# ── 1. No sleep during execute ────────────────────────────────────────────────


class TestNoSleepDuringExecute:
    """time.sleep must not be called in the execute phase.

    At 250k permissions × 0.15s = 10.4 hours of forced delay.
    """

    def test_no_sleep_during_permission_migration(self):
        analyser = _make_analyser()
        entries = _make_entries(5)
        _setup_analyser_for_permissions(analyser, _real_response(201))

        with patch("time.sleep") as mock_sleep:
            analyser._migrate_permissions(entries, MigrationStats())

        mock_sleep.assert_not_called()

    def test_no_sleep_during_team_membership_migration(self):
        analyser = _make_analyser()
        memberships = _make_memberships(5)
        analyser._target_post = lambda ep, data: _real_response(204)
        analyser._get_target_id = lambda rt, sid: sid

        with patch("time.sleep") as mock_sleep:
            analyser._migrate_team_memberships(memberships, MigrationStats())

        mock_sleep.assert_not_called()

    def test_no_sleep_even_with_many_entries(self):
        analyser = _make_analyser()
        entries = _make_entries(50)
        _setup_analyser_for_permissions(analyser, _real_response(201))

        with patch("time.sleep") as mock_sleep:
            analyser._migrate_permissions(entries, MigrationStats())

        mock_sleep.assert_not_called()


# ── 2. Transport 429 retry ────────────────────────────────────────────────────


class TestTransport429Retry:
    """429 must be in status_forcelist — transport retries, not application code."""

    def test_429_in_transport_status_forcelist(self):
        from aap_migration.iam.analyser import IAMAnalyser
        session = IAMAnalyser._create_session()
        retry = session.get_adapter("https://").max_retries
        assert 429 in retry.status_forcelist
        session.close()

    def test_502_503_504_still_in_status_forcelist(self):
        """Regression guard — existing retryable codes must not be removed."""
        from aap_migration.iam.analyser import IAMAnalyser
        session = IAMAnalyser._create_session()
        retry = session.get_adapter("https://").max_retries
        for code in (502, 503, 504):
            assert code in retry.status_forcelist, f"{code} missing from forcelist"
        session.close()

    def test_post_in_allowed_methods(self):
        from aap_migration.iam.analyser import IAMAnalyser
        session = IAMAnalyser._create_session()
        retry = session.get_adapter("https://").max_retries
        assert "POST" in retry.allowed_methods
        session.close()

    def test_retry_total_is_three(self):
        from aap_migration.iam.analyser import IAMAnalyser
        session = IAMAnalyser._create_session()
        retry = session.get_adapter("https://").max_retries
        assert retry.total == 3
        session.close()

    def test_429_exhausted_marks_permission_failed(self):
        """When transport gives up after 3 retries, _target_post returns None.
        The permission must be marked failed, not silently dropped."""
        analyser = _make_analyser()
        entries = _make_entries(1)
        _setup_analyser_for_permissions(analyser)
        analyser._target_post = lambda ep, data: None  # transport exhausted

        analyser._migrate_permissions(entries, MigrationStats())

        assert entries[0].status == "failed"
        assert "no response" in entries[0].error

    def test_429_exhausted_marks_team_membership_failed(self):
        analyser = _make_analyser()
        memberships = _make_memberships(1)
        analyser._get_target_id = lambda rt, sid: sid
        analyser._target_post = lambda ep, data: None

        analyser._migrate_team_memberships(memberships, MigrationStats())

        assert memberships[0].status == "failed"
        assert "no response" in memberships[0].error


# ── 3. Stats accuracy ─────────────────────────────────────────────────────────


class TestStatsAccuracy:
    """Stats must reflect actual outcomes — no over/under counting."""

    def test_all_succeed_stats_correct(self):
        analyser = _make_analyser()
        entries = _make_entries(10)
        _setup_analyser_for_permissions(analyser, _real_response(201))
        stats = MigrationStats()

        analyser._migrate_permissions(entries, stats)

        assert stats.permissions_migrated == 10
        assert stats.permissions_failed == 0

    def test_all_fail_stats_correct(self):
        analyser = _make_analyser()
        entries = _make_entries(10)
        _setup_analyser_for_permissions(analyser, _real_response(500))
        stats = MigrationStats()

        analyser._migrate_permissions(entries, stats)

        assert stats.permissions_migrated == 0
        assert stats.permissions_failed == 10

    def test_mixed_results_stats_correct(self):
        analyser = _make_analyser()
        entries = _make_entries(6)
        _setup_analyser_for_permissions(analyser)
        stats = MigrationStats()

        responses = [
            _real_response(201),
            _real_response(201),
            _real_response(400, {"detail": "User already has this role"}),
            _real_response(500),
            _real_response(201),
            _real_response(400, {"detail": "validation error"}),
        ]
        call_count = [0]

        def mock_post(ep, data):
            r = responses[call_count[0]]
            call_count[0] += 1
            return r

        analyser._target_post = mock_post
        analyser._migrate_permissions(entries, stats)

        assert stats.permissions_migrated == 4  # 201, 201, 400-already, 201
        assert stats.permissions_failed == 2    # 500, 400-other

    def test_team_membership_stats_correct(self):
        analyser = _make_analyser()
        memberships = _make_memberships(4)
        analyser._get_target_id = lambda rt, sid: sid
        stats = MigrationStats()

        responses = [
            _real_response(204),
            _real_response(204),
            _real_response(500),
            _real_response(400, {"detail": "User is already a member"}),
        ]
        call_count = [0]

        def mock_post(ep, data):
            r = responses[call_count[0]]
            call_count[0] += 1
            return r

        analyser._target_post = mock_post
        analyser._migrate_team_memberships(memberships, stats)

        assert stats.team_memberships_migrated == 3  # 204, 204, 400-already
        assert stats.team_memberships_failed == 1    # 500


# ── 4. Resume — skip already-processed entries ───────────────────────────────


class TestResume:
    """Already-processed entries must not be re-POSTed on resume."""

    def test_resume_skips_migrated_permissions(self):
        analyser = _make_analyser()
        entries = _make_entries(5, status="migrated")
        _setup_analyser_for_permissions(analyser)
        post_calls = []
        analyser._target_post = lambda ep, data: post_calls.append(ep) or _real_response(201)

        analyser._migrate_permissions(entries, MigrationStats())

        assert len(post_calls) == 0

    def test_resume_skips_failed_permissions(self):
        analyser = _make_analyser()
        entries = _make_entries(5, status="failed")
        _setup_analyser_for_permissions(analyser)
        post_calls = []
        analyser._target_post = lambda ep, data: post_calls.append(ep) or _real_response(201)

        analyser._migrate_permissions(entries, MigrationStats())

        assert len(post_calls) == 0

    def test_resume_skips_dry_run_permissions(self):
        analyser = _make_analyser()
        entries = _make_entries(5, status="dry_run")
        _setup_analyser_for_permissions(analyser)
        post_calls = []
        analyser._target_post = lambda ep, data: post_calls.append(ep) or _real_response(201)

        analyser._migrate_permissions(entries, MigrationStats())

        assert len(post_calls) == 0

    def test_resume_processes_only_pending_entries(self):
        analyser = _make_analyser()
        entries = _make_entries(6)
        entries[0].status = "migrated"
        entries[1].status = "failed"
        entries[2].status = "migrated"
        # entries 3,4,5 are pending (status="pending" default)
        _setup_analyser_for_permissions(analyser)
        post_calls = []
        analyser._target_post = lambda ep, data: post_calls.append(ep) or _real_response(201)

        analyser._migrate_permissions(entries, MigrationStats())

        assert len(post_calls) == 3

    def test_resume_recount_already_migrated_in_stats(self):
        """On resume, stats must reflect already-completed entries accurately."""
        analyser = _make_analyser()
        entries = _make_entries(5)
        entries[0].status = "migrated"
        entries[1].status = "migrated"
        entries[2].status = "failed"
        _setup_analyser_for_permissions(analyser, _real_response(201))
        stats = MigrationStats()

        analyser._migrate_permissions(entries, stats)

        assert stats.permissions_migrated == 4  # 2 pre-migrated + 2 new
        assert stats.permissions_failed == 1    # 1 pre-failed


# ── 5. Dry-run ────────────────────────────────────────────────────────────────


class TestDryRun:
    """dry_run=True must not issue any POSTs."""

    def test_dry_run_does_not_call_target_post(self):
        analyser = _make_analyser()
        entries = _make_entries(5)
        _setup_analyser_for_permissions(analyser)
        post_calls = []
        analyser._target_post = lambda ep, data: post_calls.append(ep) or _real_response(201)

        analyser._migrate_permissions(entries, MigrationStats(), dry_run=True)

        assert len(post_calls) == 0

    def test_dry_run_marks_entries_as_dry_run(self):
        analyser = _make_analyser()
        entries = _make_entries(3)
        _setup_analyser_for_permissions(analyser)

        analyser._migrate_permissions(entries, MigrationStats(), dry_run=True)

        assert all(e.status == "dry_run" for e in entries)

    def test_dry_run_counts_in_migrated_stats(self):
        analyser = _make_analyser()
        entries = _make_entries(3)
        _setup_analyser_for_permissions(analyser)
        stats = MigrationStats()

        analyser._migrate_permissions(entries, stats, dry_run=True)

        assert stats.permissions_migrated == 3
        assert stats.permissions_failed == 0

    def test_team_membership_dry_run_does_not_post(self):
        analyser = _make_analyser()
        memberships = _make_memberships(3)
        analyser._get_target_id = lambda rt, sid: sid
        post_calls = []
        analyser._target_post = lambda ep, data: post_calls.append(ep) or _real_response(204)

        analyser._migrate_team_memberships(memberships, MigrationStats(), dry_run=True)

        assert len(post_calls) == 0


# ── 6. Resource/principal not found ──────────────────────────────────────────


class TestNotFoundHandling:
    """Missing resources or principals must be marked failed immediately."""

    def test_resource_not_found_marks_failed(self):
        analyser = _make_analyser()
        entries = _make_entries(1)
        analyser._get_target_id = lambda rt, sid: None
        analyser._discover_target_id_by_name = lambda rt, name: None
        analyser._target_paginate = lambda ep: [{"name": "Admin", "id": 100}]
        post_calls = []
        analyser._target_post = lambda ep, data: post_calls.append(ep) or _real_response(201)

        analyser._migrate_permissions(entries, MigrationStats())

        assert entries[0].status == "failed"
        assert "not found on target" in entries[0].error
        assert len(post_calls) == 0

    def test_principal_not_found_marks_failed(self):
        analyser = _make_analyser()
        entries = _make_entries(1)

        def _id_lookup(rt, sid):
            return sid if rt == "credentials" else None

        analyser._get_target_id = _id_lookup
        analyser._discover_target_id_by_name = lambda rt, name: None
        analyser._target_paginate = lambda ep: [{"name": "Admin", "id": 100}]
        post_calls = []
        analyser._target_post = lambda ep, data: post_calls.append(ep) or _real_response(201)

        analyser._migrate_permissions(entries, MigrationStats())

        assert entries[0].status == "failed"
        assert "not found" in entries[0].error
        assert len(post_calls) == 0

    def test_role_not_in_object_roles_marks_failed(self):
        analyser = _make_analyser()
        entries = _make_entries(1)
        analyser._get_target_id = lambda rt, sid: sid
        analyser._target_paginate = lambda ep: [{"name": "Read", "id": 99}]  # Admin not present
        post_calls = []
        analyser._target_post = lambda ep, data: post_calls.append(ep) or _real_response(201)

        analyser._migrate_permissions(entries, MigrationStats())

        assert entries[0].status == "failed"
        assert "not found in object_roles" in entries[0].error
        assert len(post_calls) == 0

    def test_object_roles_404_marks_failed(self):
        analyser = _make_analyser()
        entries = _make_entries(1)
        analyser._get_target_id = lambda rt, sid: sid
        analyser._target_paginate = lambda ep: None  # 404
        post_calls = []
        analyser._target_post = lambda ep, data: post_calls.append(ep) or _real_response(201)

        analyser._migrate_permissions(entries, MigrationStats())

        assert entries[0].status == "failed"
        assert "HTTP 404" in entries[0].error
        assert len(post_calls) == 0

    def test_team_not_found_marks_membership_failed(self):
        analyser = _make_analyser()
        memberships = _make_memberships(1)
        analyser._get_target_id = lambda rt, sid: None
        analyser._discover_target_id_by_name = lambda rt, name: None
        post_calls = []
        analyser._target_post = lambda ep, data: post_calls.append(ep) or _real_response(204)

        analyser._migrate_team_memberships(memberships, MigrationStats())

        assert memberships[0].status == "failed"
        assert "Team not found" in memberships[0].error
        assert len(post_calls) == 0

    def test_user_not_found_marks_membership_failed(self):
        analyser = _make_analyser()
        memberships = _make_memberships(1)

        def _id_lookup(rt, sid):
            return sid if rt == "teams" else None

        analyser._get_target_id = _id_lookup
        analyser._discover_target_id_by_name = lambda rt, name: None
        post_calls = []
        analyser._target_post = lambda ep, data: post_calls.append(ep) or _real_response(204)

        analyser._migrate_team_memberships(memberships, MigrationStats())

        assert memberships[0].status == "failed"
        assert "User not found" in memberships[0].error
        assert len(post_calls) == 0


# ── 7. Idempotency ────────────────────────────────────────────────────────────


class TestIdempotency:
    """400 'already' responses must count as migrated, not failed."""

    def test_already_member_counts_as_migrated(self):
        analyser = _make_analyser()
        memberships = _make_memberships(2)
        analyser._get_target_id = lambda rt, sid: sid
        analyser._target_post = lambda ep, data: _real_response(
            400, {"detail": "User is already a member of this team"}
        )
        stats = MigrationStats()

        analyser._migrate_team_memberships(memberships, stats)

        assert stats.team_memberships_migrated == 2
        assert stats.team_memberships_failed == 0
        assert all(m.status == "migrated" for m in memberships)

    def test_already_has_role_counts_as_migrated(self):
        analyser = _make_analyser()
        entries = _make_entries(2)
        _setup_analyser_for_permissions(analyser)
        analyser._target_post = lambda ep, data: _real_response(
            400, {"detail": "User already has this role"}
        )
        stats = MigrationStats()

        analyser._migrate_permissions(entries, stats)

        assert stats.permissions_migrated == 2
        assert stats.permissions_failed == 0
