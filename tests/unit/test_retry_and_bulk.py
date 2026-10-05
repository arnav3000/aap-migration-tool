"""Unit tests for retry contracts + bulk mappings (review #4)."""

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


class TestRetryChildTimeout:
    def test_default_and_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aap_migration.cli.commands import retry as retry_mod

        monkeypatch.delenv("AAP_BRIDGE_JOB_TIMEOUT_SECS", raising=False)
        assert retry_mod._retry_child_timeout_secs() == retry_mod.RETRY_CHILD_TIMEOUT_SECS
        monkeypatch.setenv("AAP_BRIDGE_JOB_TIMEOUT_SECS", "42")
        assert retry_mod._retry_child_timeout_secs() == 42.0
        monkeypatch.setenv("AAP_BRIDGE_JOB_TIMEOUT_SECS", "garbage")
        assert retry_mod._retry_child_timeout_secs() == retry_mod.RETRY_CHILD_TIMEOUT_SECS


class TestRunChildInGroup:
    def test_timeout_kills_group(self) -> None:
        from aap_migration.cli.commands.retry import _run_child_in_group

        with pytest.raises(subprocess.TimeoutExpired):
            _run_child_in_group(["sleep", "30"], timeout=0.2)

    def test_success_returns_code(self) -> None:
        from aap_migration.cli.commands.retry import _run_child_in_group

        assert _run_child_in_group(["true"], timeout=5) == 0


class TestMarkTypeFailed:
    def _state(self, tmp_path: Path) -> MagicMock:
        from aap_migration.migration import database as db

        url = f"sqlite:///{tmp_path}/retry.db"
        db.init_database(url)
        state = MagicMock()
        state.database_url = url
        return state

    def test_pending_remarked_failed_with_note(self, tmp_path: Path) -> None:
        from aap_migration.cli.commands.retry import _mark_type_failed
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import MigrationProgress

        state = self._state(tmp_path)
        with get_session(state.database_url) as s:
            s.add(
                MigrationProgress(
                    resource_type="hosts",
                    source_id=1,
                    status="pending",
                    phase="import",
                    source_name="h1",
                )
            )
            s.commit()
        assert _mark_type_failed(state, "hosts", note="timeout") is True
        with get_session(state.database_url) as s:
            row = s.query(MigrationProgress).filter_by(resource_type="hosts", source_id=1).one()
            assert row.status == "failed"
            assert "timeout" in (row.error_message or "")

    def test_in_progress_remarked_failed(self, tmp_path: Path) -> None:
        # A timeout SIGKILLs the child mid-row: rows stuck in_progress must
        # flip back to failed or every future retry skips them (#4).
        from aap_migration.cli.commands.retry import _mark_type_failed
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import MigrationProgress

        state = self._state(tmp_path)
        with get_session(state.database_url) as s:
            s.add(
                MigrationProgress(
                    resource_type="hosts",
                    source_id=7,
                    status="in_progress",
                    phase="import",
                    source_name="h7",
                )
            )
            s.commit()
        assert _mark_type_failed(state, "hosts", note="timeout") is True
        with get_session(state.database_url) as s:
            row = s.query(MigrationProgress).filter_by(resource_type="hosts", source_id=7).one()
            assert row.status == "failed"

    def test_note_none_preserves_error_message(self, tmp_path: Path) -> None:
        from aap_migration.cli.commands.retry import _mark_type_failed
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import MigrationProgress

        state = self._state(tmp_path)
        with get_session(state.database_url) as s:
            s.add(
                MigrationProgress(
                    resource_type="hosts",
                    source_id=9,
                    status="pending",
                    phase="import",
                    source_name="h9",
                    error_message="original boom",
                )
            )
            s.commit()
        assert _mark_type_failed(state, "hosts") is True
        with get_session(state.database_url) as s:
            row = s.query(MigrationProgress).filter_by(resource_type="hosts", source_id=9).one()
            assert row.status == "failed"
            assert row.error_message == "original boom"


class TestFlipAndSweep:
    def _state(self, tmp_path: Path) -> MagicMock:
        from aap_migration.migration import database as db

        url = f"sqlite:///{tmp_path}/strand.db"
        db.init_database(url)
        state = MagicMock()
        state.database_url = url
        return state

    def _seed(self, url: str, status: str, source_id: int) -> None:
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import MigrationProgress

        with get_session(url) as s:
            s.add(
                MigrationProgress(
                    resource_type="hosts",
                    source_id=source_id,
                    status=status,
                    phase="import",
                    source_name=f"h{source_id}",
                )
            )
            s.commit()

    def test_flip_scoped_per_type(self, tmp_path: Path) -> None:
        from aap_migration.cli.commands.retry import _flip_type_to_pending
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import MigrationProgress

        state = self._state(tmp_path)
        self._seed(state.database_url, "failed", 1)
        with get_session(state.database_url) as s:
            s.add(
                MigrationProgress(
                    resource_type="credentials",
                    source_id=2,
                    status="failed",
                    phase="import",
                    source_name="c2",
                )
            )
            s.commit()
        assert _flip_type_to_pending(state, "hosts") == 1
        with get_session(state.database_url) as s:
            hosts = s.query(MigrationProgress).filter_by(resource_type="hosts").one()
            creds = s.query(MigrationProgress).filter_by(resource_type="credentials").one()
            assert hosts.status == "pending"
            assert creds.status == "failed"

    def test_sweep_recovers_stale_keeps_fresh(self, tmp_path: Path) -> None:
        from datetime import datetime, timedelta

        from sqlalchemy import text

        from aap_migration.cli.commands.retry import _sweep_stranded_to_failed
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import MigrationProgress

        state = self._state(tmp_path)
        self._seed(state.database_url, "pending", 1)
        self._seed(state.database_url, "in_progress", 2)
        self._seed(state.database_url, "pending", 3)
        stale = datetime.utcnow() - timedelta(hours=2)
        with get_session(state.database_url) as s:
            s.execute(
                text("UPDATE migration_progress SET updated_at = :ts WHERE source_id IN (1, 2)"),
                {"ts": stale},
            )
            s.commit()
        assert _sweep_stranded_to_failed(state, ["hosts"], 3600.0) == 2
        with get_session(state.database_url) as s:
            by_id = {
                r.source_id: r
                for r in s.query(MigrationProgress).filter_by(resource_type="hosts").all()
            }
            assert by_id[1].status == "failed"
            assert by_id[2].status == "failed"
            assert "recovered" in (by_id[1].error_message or "")
            assert by_id[3].status == "pending"

    def test_sweep_empty_rtypes_noop(self, tmp_path: Path) -> None:
        from aap_migration.cli.commands.retry import _sweep_stranded_to_failed

        state = self._state(tmp_path)
        assert _sweep_stranded_to_failed(state, [], 3600.0) == 0


class TestRetryFailedCommand:
    def _ctx(self, tmp_path: Path) -> Any:
        from types import SimpleNamespace

        from aap_migration.config import StateConfig
        from aap_migration.migration import database as db
        from aap_migration.migration.state import MigrationState

        url = f"sqlite:///{tmp_path}/cmd.db"
        db.init_database(url)
        state = MigrationState(config=StateConfig(db_path=url))
        return SimpleNamespace(
            config_path=tmp_path / "cfg.yaml",
            config=SimpleNamespace(paths=SimpleNamespace(transform_dir=str(tmp_path))),
            migration_state=state,
        )

    def _seed_failed(self, state: Any, n: int = 2) -> None:
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import MigrationProgress

        with get_session(state.database_url) as s:
            for i in range(n):
                s.add(
                    MigrationProgress(
                        resource_type="hosts",
                        source_id=100 + i,
                        status="failed",
                        phase="import",
                        source_name=f"h{100 + i}",
                        error_message="boom",
                    )
                )
            s.commit()

    def test_all_success_exit_zero(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from aap_migration.cli.commands.retry import retry_failed

        ctx = self._ctx(tmp_path)
        self._seed_failed(ctx.migration_state)
        with patch("aap_migration.cli.commands.retry._run_child_in_group", return_value=0):
            result = runner_run(retry_failed, ["-r", "hosts", "-y"], ctx)
        assert result.exit_code == 0, result.output

    def test_failed_type_raises_naming_type(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from aap_migration.cli.commands.retry import retry_failed
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import MigrationProgress

        ctx = self._ctx(tmp_path)
        self._seed_failed(ctx.migration_state)
        with patch("aap_migration.cli.commands.retry._run_child_in_group", return_value=1):
            result = runner_run(retry_failed, ["-r", "hosts", "-y"], ctx)
        assert result.exit_code != 0
        assert "hosts" in (result.output or "")
        with get_session(ctx.migration_state.database_url) as s:
            rows = s.query(MigrationProgress).filter_by(resource_type="hosts").all()
            assert rows and all(r.status == "failed" for r in rows)

    def test_no_failed_reports_nothing(self, tmp_path: Path) -> None:
        from aap_migration.cli.commands.retry import retry_failed

        ctx = self._ctx(tmp_path)
        result = runner_run(retry_failed, ["-y"], ctx)
        assert result.exit_code == 0, result.output
        assert "No failed resources" in (result.output or "")


def runner_run(cmd: Any, args: list[str], obj: Any) -> Any:
    """Invoke a click command with a stub context object."""
    from click.testing import CliRunner

    runner = CliRunner()
    return runner.invoke(cmd, args, obj=obj)


class TestIamRetryDelayCap:
    def _resp(self, retry_after: Any) -> MagicMock:
        resp = MagicMock()
        resp.headers = {"Retry-After": retry_after} if retry_after is not None else {}
        return resp

    def test_absurd_retry_after_capped(self) -> None:
        from aap_migration.iam.analyser import IAMAnalyser

        assert IAMAnalyser._retry_delay(self._resp("99999999"), 0, 1.0) == 60.0

    def test_http_date_falls_back_to_backoff(self) -> None:
        # Non-numeric Retry-After is not slept verbatim; exponential backoff applies.
        from aap_migration.iam.analyser import IAMAnalyser

        assert IAMAnalyser._retry_delay(self._resp("Wed, 01 Jan 2040 00:00:00 GMT"), 2, 1.0) == 4.0

    def test_small_retry_after_honored(self) -> None:
        from aap_migration.iam.analyser import IAMAnalyser

        assert IAMAnalyser._retry_delay(self._resp("5"), 0, 1.0) == 5.0

    def test_garbage_falls_back_to_backoff(self) -> None:
        from aap_migration.iam.analyser import IAMAnalyser

        assert IAMAnalyser._retry_delay(self._resp("garbage"), 2, 1.0) == 4.0

    def test_missing_header_backoff(self) -> None:
        from aap_migration.iam.analyser import IAMAnalyser

        assert IAMAnalyser._retry_delay(self._resp(None), 1, 1.0) == 2.0


class TestRetryRateLimitCap:
    async def test_retry_after_capped_at_max_wait(self) -> None:
        from aap_migration.client.exceptions import RateLimitError
        from aap_migration.utils.retry import retry_with_rate_limit_handling

        calls = {"n": 0}

        async def flaky() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RateLimitError("Rate limit exceeded", 429, {}, retry_after=1000)
            return "ok"

        slept: list[float] = []

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)

        import asyncio as _asyncio

        orig_sleep = _asyncio.sleep
        _asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            result = await retry_with_rate_limit_handling(flaky, max_attempts=3, max_wait=120)
        finally:
            _asyncio.sleep = orig_sleep
        assert result == "ok"
        assert slept == [120]

    async def test_small_retry_after_honored_exactly(self) -> None:
        from aap_migration.client.exceptions import RateLimitError
        from aap_migration.utils.retry import retry_with_rate_limit_handling

        calls = {"n": 0}

        async def flaky() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RateLimitError("Rate limit exceeded", 429, {}, retry_after=5)
            return "ok"

        slept: list[float] = []

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)

        import asyncio as _asyncio

        orig_sleep = _asyncio.sleep
        _asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            result = await retry_with_rate_limit_handling(flaky, max_attempts=3, max_wait=120)
        finally:
            _asyncio.sleep = orig_sleep
        assert result == "ok"
        assert slept == [5]


class TestInitDatabasePerUrl:
    def test_second_url_returns_own_engine(self, tmp_path: Path) -> None:
        from aap_migration.migration import database as db

        url_a = f"sqlite:///{tmp_path}/a.db"
        url_b = f"sqlite:///{tmp_path}/b.db"
        engine_a = db.init_database(url_a)
        engine_b = db.init_database(url_b)
        assert engine_b is not engine_a
        assert engine_b is db._engines[url_b]
        assert engine_a is db._engines[url_a]


class TestRegistryEviction:
    def _isolated(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from aap_migration.migration import database as db

        monkeypatch.setattr(db, "_engines", {})
        monkeypatch.setattr(db, "_factories", {})
        monkeypatch.setattr(db, "_engine", None)
        monkeypatch.setattr(db, "_SessionFactory", None)
        monkeypatch.setattr(db, "_legacy_url", None)
        return db

    def test_eviction_skips_legacy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = self._isolated(monkeypatch)
        monkeypatch.setattr(db, "_MAX_REGISTRY_ENTRIES", 3)
        urls = [f"sqlite:///{tmp_path}/ev{i}.db" for i in range(4)]
        legacy_engine = db.init_database(urls[0])
        assert db._legacy_url == urls[0]
        db.init_database(urls[1])
        db.init_database(urls[2])
        assert set(db._engines) == set(urls[:3])
        db.init_database(urls[3])
        assert urls[0] in db._engines
        assert db._engines[urls[0]] is legacy_engine
        assert len(db._engines) == 3
        assert urls[1] not in db._engines
        assert db.get_engine(None) is legacy_engine

    def test_all_legacy_pinned_grows_past_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = self._isolated(monkeypatch)
        monkeypatch.setattr(db, "_MAX_REGISTRY_ENTRIES", 1)
        url_a = f"sqlite:///{tmp_path}/pin_a.db"
        url_b = f"sqlite:///{tmp_path}/pin_b.db"
        engine_a = db.init_database(url_a)
        db.init_database(url_b)
        assert db._engines[url_a] is engine_a
        assert len(db._engines) == 2

    def test_legacy_globals_frozen_to_first_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aap_migration.migration.models import MigrationProgress

        db = self._isolated(monkeypatch)
        url_a = f"sqlite:///{tmp_path}/froza.db"
        url_b = f"sqlite:///{tmp_path}/frozb.db"
        engine_a = db.init_database(url_a)
        db.init_database(url_b)
        assert db.get_engine(None) is engine_a
        with db.get_session(url_a) as s:
            s.add(
                MigrationProgress(
                    resource_type="hosts",
                    source_id=1,
                    status="completed",
                    phase="import",
                    source_name="h1",
                )
            )
            s.commit()
        with db.get_session(url_b) as s:
            assert s.query(MigrationProgress).count() == 0

    def test_sqlite_wal_enabled(self, tmp_path: Path) -> None:
        from aap_migration.migration import database as db

        url = f"sqlite:///{tmp_path}/wal.db"
        engine = db.init_database(url)
        with engine.connect() as conn:
            mode = conn.exec_driver_sql("PRAGMA journal_mode;").scalar()
        assert mode is not None and str(mode).upper() == "WAL"


class TestBulkMappings:
    def _state(self, tmp_path: Path) -> Any:
        from aap_migration.config import StateConfig
        from aap_migration.migration import database as db
        from aap_migration.migration.state import MigrationState

        url = f"sqlite:///{tmp_path}/bulk.db"
        db.init_database(url)
        return MigrationState(config=StateConfig(db_path=url))

    def test_empty_and_none(self, tmp_path: Path) -> None:
        state = self._state(tmp_path)
        assert state.bulk_has_source_mappings("hosts", set()) == set()
        assert state.bulk_has_source_mappings("hosts", [None]) == set()

    def test_subset_and_non_numeric_skipped(self, tmp_path: Path) -> None:
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import IDMapping

        state = self._state(tmp_path)
        with get_session(state.database_url) as s:
            s.add(IDMapping(resource_type="inventories", source_id=1, source_name="i1"))
            s.add(IDMapping(resource_type="inventories", source_id=2, source_name="i2"))
            s.commit()
        found = state.bulk_has_source_mappings("inventories", {1, 2, 99, "nope", None})
        assert found == {1, 2}

    def test_present_source_ids_returns_raw_subset(self, tmp_path: Path) -> None:
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import IDMapping

        state = self._state(tmp_path)
        with get_session(state.database_url) as s:
            s.add(IDMapping(resource_type="inventories", source_id=5, source_name="i5"))
            s.commit()
        # Mixed str/int raw ids: mapped originals returned, plain `in` works.
        present = state.present_source_ids("inventories", {"5", 5, 99, "nope", None})
        assert "5" in present and 5 in present
        assert 99 not in present and "nope" not in present
        assert state.present_source_ids("inventories", set()) == set()


class TestStateHelpers:
    def _state(self, tmp_path: Path) -> Any:
        from aap_migration.config import StateConfig
        from aap_migration.migration import database as db
        from aap_migration.migration.state import MigrationState

        url = f"sqlite:///{tmp_path}/helpers.db"
        db.init_database(url)
        return MigrationState(config=StateConfig(db_path=url))

    def _seed_progress(self, state: Any, rtype: str, sid: int, status: str) -> None:
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import MigrationProgress

        with get_session(state.database_url) as s:
            s.add(
                MigrationProgress(
                    resource_type=rtype,
                    source_id=sid,
                    status=status,
                    phase="import",
                    source_name=f"n{sid}",
                )
            )
            s.commit()

    def test_get_source_name_hit_and_miss(self, tmp_path: Path) -> None:
        state = self._state(tmp_path)
        self._seed_progress(state, "hosts", 1, "completed")
        assert state.get_source_name("hosts", 1) == "n1"
        assert state.get_source_name("hosts", 999) is None

    def test_get_failed_source_ids(self, tmp_path: Path) -> None:
        state = self._state(tmp_path)
        self._seed_progress(state, "hosts", 1, "failed")
        self._seed_progress(state, "hosts", 2, "completed")
        assert state.get_failed_source_ids("hosts") == {1}

    def test_get_failed_resource_types(self, tmp_path: Path) -> None:
        state = self._state(tmp_path)
        self._seed_progress(state, "hosts", 1, "failed")
        self._seed_progress(state, "credentials", 2, "failed")
        self._seed_progress(state, "hosts", 3, "completed")
        assert state.get_failed_resource_types() == ["credentials", "hosts"]

    def test_append_warnings_only_completed(self, tmp_path: Path) -> None:
        from aap_migration.migration.database import get_session
        from aap_migration.migration.models import MigrationProgress

        state = self._state(tmp_path)
        self._seed_progress(state, "hosts", 1, "completed")
        self._seed_progress(state, "hosts", 2, "failed")
        state.append_notification_warnings("hosts", {1: ["w1"], 2: ["w2"]})
        with get_session(state.database_url) as s:
            done = s.query(MigrationProgress).filter_by(source_id=1).one()
            failed = s.query(MigrationProgress).filter_by(source_id=2).one()
            assert done.error_message is not None and "WARNING: w1" in done.error_message
            assert failed.error_message is None or "w2" not in failed.error_message
