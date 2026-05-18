from __future__ import annotations

import builtins
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from aap_migration.cli import main
from aap_migration.cli.commands import serve as serve_module


def test_cli_without_subcommand_launches_interactive_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[object] = []
    runner = CliRunner()

    monkeypatch.setattr(main, "configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(main, "interactive_menu", lambda ctx: called.append(ctx.obj))

    result = runner.invoke(main.cli, [])

    assert result.exit_code == 0
    assert len(called) == 1
    assert called[0].log_level == "ERROR"


def test_main_returns_zero_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "cli", lambda standalone_mode=False: None)

    assert main.main() == 0


def test_main_returns_click_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_click(standalone_mode: bool = False) -> None:
        raise click.ClickException("bad input")

    monkeypatch.setattr(main, "cli", raise_click)

    assert main.main() == 1


def test_main_returns_one_for_unexpected_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_unexpected(standalone_mode: bool = False) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "cli", raise_unexpected)

    assert main.main() == 1


def test_serve_exits_when_uvicorn_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = runner.invoke(serve_module.serve, [])

    assert result.exit_code == 1
    assert "uvicorn is not installed" in result.output


def test_serve_reload_mode_uses_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    run_calls: list[tuple[object, dict[str, object]]] = []

    monkeypatch.setitem(
        __import__("sys").modules,
        "uvicorn",
        SimpleNamespace(run=lambda app, **kwargs: run_calls.append((app, kwargs))),
    )
    monkeypatch.setattr(
        "aap_migration.api.dependencies.get_db_url",
        lambda: "sqlite:///reload.db",
    )

    result = runner.invoke(serve_module.serve, ["--host", "0.0.0.0", "--port", "9000", "--reload"])

    assert result.exit_code == 0
    assert "Using database: sqlite:///reload.db" in result.output
    assert run_calls == [
        (
            "aap_migration.api.app:create_app",
            {
                "factory": True,
                "host": "0.0.0.0",
                "port": 9000,
                "reload": True,
                "reload_dirs": ["src"],
            },
        )
    ]


def test_serve_non_reload_mode_builds_app_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    app = object()
    run_calls: list[tuple[object, dict[str, object]]] = []

    monkeypatch.setitem(
        __import__("sys").modules,
        "uvicorn",
        SimpleNamespace(run=lambda target, **kwargs: run_calls.append((target, kwargs))),
    )
    monkeypatch.setattr(
        "aap_migration.api.dependencies.get_db_url",
        lambda: "sqlite:///serve.db",
    )
    monkeypatch.setattr("aap_migration.api.app.create_app", lambda db_url=None: app)

    result = runner.invoke(serve_module.serve, ["--host", "127.0.0.1", "--port", "8123"])

    assert result.exit_code == 0
    assert run_calls == [(app, {"host": "127.0.0.1", "port": 8123})]
