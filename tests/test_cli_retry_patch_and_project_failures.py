from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from aap_migration.cli.commands import patch_projects as patch_module
from aap_migration.cli.commands import project_failures as failures_module
from aap_migration.cli.commands import retry as retry_module


def _unwrap_callback(command) -> object:
    callback = command.callback
    while hasattr(callback, "__wrapped__"):
        callback = callback.__wrapped__
    return callback


def test_analyze_project_failures_generates_manual_report(tmp_path: Path, monkeypatch) -> None:
    projects_dir = tmp_path / "xformed" / "projects"
    projects_dir.mkdir(parents=True)
    (projects_dir / "projects_0001.json").write_text(
        json.dumps(
            [
                {"id": 1, "_source_id": 1, "name": "Manual Project", "organization": 5},
                {
                    "id": 2,
                    "_source_id": 2,
                    "name": "SCM Project",
                    "organization": 6,
                    "_deferred_scm_details": {
                        "scm_url": "https://git.example/repo.git",
                        "scm_type": "git",
                        "scm_branch": "main",
                        "credential": 9,
                    },
                },
                {
                    "id": 3,
                    "_source_id": 3,
                    "name": "Imported SCM",
                    "_deferred_scm_details": {
                        "scm_url": "https://git.example/two.git",
                        "scm_type": "git",
                    },
                },
            ]
        )
    )

    mapped_ids = {1: 101, 3: 103}
    messages = []
    monkeypatch.setattr(failures_module, "echo_info", lambda msg: messages.append(("info", msg)))
    monkeypatch.setattr(
        failures_module, "echo_success", lambda msg: messages.append(("success", msg))
    )
    monkeypatch.setattr(
        failures_module, "echo_warning", lambda msg: messages.append(("warning", msg))
    )

    ctx = SimpleNamespace(
        config=SimpleNamespace(
            paths=SimpleNamespace(transform_dir=str(tmp_path / "xformed")),
            target=SimpleNamespace(url="https://target.example.com/api/controller"),
        ),
        migration_state=SimpleNamespace(
            get_mapped_id=lambda rtype, source_id: mapped_ids.get(source_id)
        ),
    )
    output = tmp_path / "report.md"
    failures_module.analyze_project_failures.callback.__wrapped__.__wrapped__.__wrapped__(
        ctx, output
    )

    report_text = output.read_text()
    assert "Projects That Failed to Import" in report_text
    assert "Imported Projects That Need SCM Patching" in report_text
    assert "https://target.example.com/api/v2/projects/?name=SCM%20Project" in report_text
    assert any(kind == "warning" for kind, _ in messages)


def test_retry_failed_and_status_commands(monkeypatch) -> None:
    class FakeConsole:
        def __init__(self) -> None:
            self.printed = []

        def print(self, item=""):
            self.printed.append(item)

    class ProgressRecord:
        def __init__(self) -> None:
            self.status = "failed"

    progress_records = {
        ("projects", 1): ProgressRecord(),
        ("credentials", 2): ProgressRecord(),
    }

    class FailedRowsQuery:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            return self.rows

    class StatusRowsQuery(FailedRowsQuery):
        def group_by(self, *args, **kwargs):
            return self

    class ProgressQuery:
        def __init__(self):
            self.key = None

        def filter_by(self, resource_type=None, source_id=None):
            self.key = (resource_type, source_id)
            return self

        def first(self):
            return progress_records.get(self.key)

    class FakeSession:
        def __init__(self, rows, grouped: bool = False):
            self.rows = rows
            self.grouped = grouped
            self.commits = 0

        def query(self, *args):
            if len(args) == 1:
                return ProgressQuery()
            if self.grouped:
                return StatusRowsQuery(self.rows)
            return FailedRowsQuery(self.rows)

        def commit(self):
            self.commits += 1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    failed_rows = [
        ("projects", 1, "ProjA", "boom"),
        ("credentials", 2, "CredA", "dup"),
    ]
    status_rows = [
        ("projects", "completed", 3),
        ("projects", "failed", 1),
        ("credentials", None, 2),
    ]
    session_holder = {"session": FakeSession(failed_rows)}
    monkeypatch.setattr(
        "aap_migration.migration.database.get_session", lambda url: session_holder["session"]
    )
    fake_console = FakeConsole()
    monkeypatch.setattr(retry_module, "Console", lambda: fake_console)
    messages = []
    monkeypatch.setattr(retry_module, "echo_info", lambda msg: messages.append(("info", msg)))
    monkeypatch.setattr(retry_module, "echo_success", lambda msg: messages.append(("success", msg)))
    monkeypatch.setattr(retry_module, "echo_warning", lambda msg: messages.append(("warning", msg)))
    monkeypatch.setattr(retry_module, "echo_error", lambda msg: messages.append(("error", msg)))
    monkeypatch.setattr(
        retry_module.subprocess,
        "run",
        lambda cmd, check=False, capture_output=False, text=True: SimpleNamespace(
            returncode=0 if "projects" in cmd else 1
        ),
    )

    ctx = SimpleNamespace(
        config=SimpleNamespace(paths=SimpleNamespace(transform_dir="xformed")),
        config_path=Path("config.yaml"),
        migration_state=SimpleNamespace(database_url="sqlite:///ignored.db"),
    )

    retry_failed_callback = _unwrap_callback(retry_module.retry_failed)
    retry_failed_callback(ctx, (), None, False, True)
    assert progress_records[("projects", 1)].status is None
    assert progress_records[("credentials", 2)].status is None
    assert any(kind == "success" and "Retry complete" in msg for kind, msg in messages)

    session_holder["session"] = FakeSession(status_rows, grouped=True)
    retry_status_callback = _unwrap_callback(retry_module.retry_status)
    retry_status_callback(ctx, ())
    assert fake_console.printed


def test_patch_projects_async_and_command_wrapper(tmp_path: Path, monkeypatch) -> None:
    projects_dir = tmp_path / "xformed" / "projects"
    projects_dir.mkdir(parents=True)
    (projects_dir / "projects_0001.json").write_text(
        json.dumps(
            [
                {
                    "_source_id": 1,
                    "name": "Proj One",
                    "_deferred_scm_details": {
                        "scm_type": "git",
                        "scm_url": "https://git.example/repo.git",
                        "credential": 9,
                    },
                },
                {
                    "_source_id": 2,
                    "name": "Proj Missing Mapping",
                    "_deferred_scm_details": {
                        "scm_type": "git",
                        "scm_url": "https://git.example/other.git",
                    },
                },
            ]
        )
    )

    class FakeProgress:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def set_total_phases(self, count):
            return None

        def initialize_and_start_single_phase(self, *args):
            return None

        def start_phase(self, *args):
            return None

        def update_phase(self, *args):
            return None

        def complete_phase(self, *args):
            return None

    async def fake_wait_for_project_sync(client, project_ids, timeout, poll_interval):
        return (len(project_ids), 0, [])

    async def fake_sleep(_seconds):
        return None

    patched = []
    monkeypatch.setattr(patch_module, "MigrationProgressDisplay", FakeProgress)
    monkeypatch.setattr(patch_module, "wait_for_project_sync", fake_wait_for_project_sync)
    monkeypatch.setattr(patch_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(patch_module, "echo_info", lambda msg: None)
    monkeypatch.setattr(patch_module, "echo_success", lambda msg: None)
    monkeypatch.setattr(patch_module, "echo_warning", lambda msg: None)

    ctx = SimpleNamespace(
        migration_state=SimpleNamespace(
            get_mapped_id=lambda rtype, source_id: {1: 101, 9: 909}.get(source_id)
        ),
        target_client=SimpleNamespace(
            patch=lambda endpoint, json_data=None: asyncio.sleep(
                0, result=patched.append((endpoint, json_data))
            )
        ),
        config=SimpleNamespace(performance=SimpleNamespace(project_sync_poll_interval=0)),
    )

    asyncio.run(
        patch_module.patch_project_scm_details(
            ctx,
            tmp_path / "xformed",
            batch_size=1,
            interval=0,
            progress_display=None,
            project_source_ids={1, 2},
        )
    )
    assert patched[0][0] == "projects/101/"
    assert patched[0][1]["credential"] == 909

    captured = []

    async def fake_patch_project_scm_details(ctx, input_dir, batch_size, interval):
        captured.append((input_dir, batch_size, interval))

    monkeypatch.setattr(patch_module, "patch_project_scm_details", fake_patch_project_scm_details)
    patch_callback = _unwrap_callback(patch_module.patch_projects)
    patch_callback(
        SimpleNamespace(
            config=SimpleNamespace(
                paths=SimpleNamespace(transform_dir=str(tmp_path / "xformed")),
                performance=SimpleNamespace(
                    project_patch_batch_size=5, project_patch_batch_interval=10
                ),
            )
        ),
        None,
        None,
        None,
    )
    assert captured == [(tmp_path / "xformed", 5, 10)]
