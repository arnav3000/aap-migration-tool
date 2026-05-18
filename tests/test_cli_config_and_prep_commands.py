from __future__ import annotations

import asyncio as py_asyncio
import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner
from rich.logging import RichHandler

import aap_migration.cli.commands.config as config_module
import aap_migration.cli.commands.prep as prep_module
from aap_migration.utils.version_validation import VersionValidationError


class PrepClient:
    def __init__(self, name: str, version: str, calls: list[tuple]):
        self.name = name
        self.version = version
        self.calls = calls

    async def get(self, endpoint: str):
        self.calls.append((self.name, "get", endpoint))
        return {"ok": True}

    async def get_version(self):
        self.calls.append((self.name, "version"))
        return self.version


def build_config(
    tmp_path: Path,
    *,
    batch_sizes: dict[str, int] | None = None,
    max_concurrent: int = 10,
    rate_limit: int = 5,
):
    return SimpleNamespace(
        source=SimpleNamespace(url="https://source.example/api/v2/", verify_ssl=True),
        target=SimpleNamespace(url="https://target.example/api/v2/", verify_ssl=False),
        state=SimpleNamespace(db_path=tmp_path / "state" / "migration.db"),
        performance=SimpleNamespace(
            batch_sizes=batch_sizes or {"default": 10, "hosts": 20},
            max_concurrent=max_concurrent,
            rate_limit=rate_limit,
        ),
        ignored_endpoints={
            "common": ["common/"],
            "source": ["source-only/"],
            "target": ["target-only/"],
        },
    )


def build_ctx(tmp_path: Path, config=None, *, source_client=None, target_client=None):
    config = config or build_config(tmp_path)
    return SimpleNamespace(
        config_path=tmp_path / "config.yaml",
        config=config,
        source_client=source_client if source_client is not None else object(),
        target_client=target_client if target_client is not None else object(),
    )


def install_prep_success_patches(
    monkeypatch: pytest.MonkeyPatch,
    captures: dict[str, list],
):
    @contextmanager
    def fake_step_progress(message: str):
        captures["steps"].append(message)
        yield

    async def fake_discover(client, api_version, ignored_endpoints):
        captures["discover"].append((client.name, api_version, tuple(ignored_endpoints)))
        return {"endpoints": {"organizations": {"url": "organizations/"}}}

    async def fake_generate(client, endpoints):
        captures["generate"].append((client.name, tuple(endpoints["endpoints"])))
        return {"schemas": {"organizations": {"fields": {"name": {"type": "string"}}}}}

    def fake_compare(source_schema, target_schema):
        captures["compare"].append((source_schema, target_schema))
        return {
            "transformations": {
                "organizations": {
                    "fields_added": ["is_default"],
                    "fields_removed": ["legacy"],
                },
                "users": {
                    "fields_added": [],
                    "fields_removed": [],
                    "requires_manual_verification": True,
                },
            }
        }

    monkeypatch.setattr(prep_module, "step_progress", fake_step_progress)
    monkeypatch.setattr(prep_module, "discover_endpoints", fake_discover)
    monkeypatch.setattr(prep_module, "generate_schema", fake_generate)
    monkeypatch.setattr(prep_module, "compare_schemas", fake_compare)
    monkeypatch.setattr(
        prep_module,
        "save_endpoints",
        lambda payload, path: captures["saves"].append(("endpoints", path.name, payload)),
    )
    monkeypatch.setattr(
        prep_module,
        "save_schema",
        lambda payload, path: captures["saves"].append(("schema", path.name, payload)),
    )
    monkeypatch.setattr(
        prep_module,
        "save_comparison",
        lambda payload, path: captures["saves"].append(("comparison", path.name, payload)),
    )
    monkeypatch.setattr(
        prep_module,
        "validate_version_compatibility",
        lambda source, target: captures["versions"].append((source, target)),
    )
    monkeypatch.setattr(
        prep_module, "echo_success", lambda message: captures["success"].append(message)
    )


def test_config_helpers_cover_validation_branches(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    successes: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    printed: list[tuple[str, list[str], list[list[object]]]] = []

    monkeypatch.setattr(config_module, "echo_success", lambda message: successes.append(message))
    monkeypatch.setattr(config_module, "echo_warning", lambda message: warnings.append(message))
    monkeypatch.setattr(config_module, "echo_error", lambda message: errors.append(message))
    monkeypatch.setattr(
        config_module,
        "print_table",
        lambda title, headers, rows: printed.append((title, headers, rows)),
    )

    config = build_config(tmp_path)
    config_module._display_config_summary(config)
    assert printed[0][0] == "Configuration Summary"
    assert printed[0][2][0] == ["Source URL", config.source.url]

    config_module._validate_paths(config)
    assert (tmp_path / "state").is_dir()
    assert any("Created database directory" in message for message in successes)

    successes.clear()
    config_module._validate_paths(config)
    assert any("Database directory exists" in message for message in successes)

    bad_parent = tmp_path / "not-a-dir"
    bad_parent.write_text("x")
    bad_config = build_config(tmp_path)
    bad_config.state.db_path = bad_parent / "migration.db"
    with pytest.raises(click.ClickException, match="Invalid database directory"):
        config_module._validate_paths(bad_config)

    denied_config = build_config(tmp_path)
    denied_config.state.db_path = tmp_path / "denied" / "migration.db"

    def failing_mkdir(self, parents=False, exist_ok=False):
        raise OSError("permission denied")

    with pytest.MonkeyPatch.context() as inner:
        inner.setattr(Path, "mkdir", failing_mkdir)
        with pytest.raises(click.ClickException, match="Failed to create database directory"):
            config_module._validate_paths(denied_config)

    successes.clear()
    warnings.clear()
    warn_config = build_config(
        tmp_path,
        batch_sizes={"default": 10, "hosts": 250},
        max_concurrent=60,
        rate_limit=25,
    )
    config_module._validate_settings(warn_config)
    assert any("Host batch size" in message for message in warnings)
    assert any("High concurrency" in message for message in warnings)
    assert successes[-1] == "All settings are valid"

    invalid_batch = build_config(tmp_path, batch_sizes={"default": 0})
    with pytest.raises(click.ClickException, match="Batch size must be positive"):
        config_module._validate_settings(invalid_batch)

    invalid_concurrency = build_config(tmp_path, max_concurrent=0)
    with pytest.raises(click.ClickException, match="Max concurrent requests must be positive"):
        config_module._validate_settings(invalid_concurrency)

    invalid_rate = build_config(tmp_path, rate_limit=0)
    with pytest.raises(click.ClickException, match="Rate limit must be positive"):
        config_module._validate_settings(invalid_rate)

    assert any("Invalid batch size" in message for message in errors)
    assert any("Invalid max concurrent requests" in message for message in errors)
    assert any("Invalid rate limit" in message for message in errors)


def test_config_commands_and_connectivity_helper(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    ctx = build_ctx(tmp_path)
    helper_calls: list[str] = []

    with pytest.MonkeyPatch.context() as command_patches:
        command_patches.setattr(
            config_module,
            "_display_config_summary",
            lambda config: helper_calls.append("summary"),
        )
        command_patches.setattr(
            config_module,
            "_validate_paths",
            lambda config: helper_calls.append("paths"),
        )
        command_patches.setattr(
            config_module,
            "_validate_settings",
            lambda config: helper_calls.append("settings"),
        )
        command_patches.setattr(
            config_module,
            "_test_connectivity",
            lambda ctx: helper_calls.append("connectivity"),
        )

        result = runner.invoke(config_module.validate, ["--check-connectivity"], obj=ctx)
        assert result.exit_code == 0
        assert helper_calls == ["summary", "paths", "settings", "connectivity"]
        assert "Configuration is valid!" in result.output

        result = runner.invoke(config_module.show, obj=ctx)
        assert result.exit_code == 0
        assert "Source Configuration:" in result.output
        assert "Target Configuration:" in result.output
        assert "Token: **************************************** (masked)" in result.output

    config_module.config.callback()

    info: list[str] = []
    success: list[str] = []
    error: list[str] = []
    monkeypatch.setattr(config_module, "echo_info", lambda message: info.append(message))
    monkeypatch.setattr(config_module, "echo_success", lambda message: success.append(message))
    monkeypatch.setattr(config_module, "echo_error", lambda message: error.append(message))

    success_ctx = build_ctx(tmp_path)
    config_module._test_connectivity(success_ctx)
    assert "Testing source AAP connection..." in info
    assert any("Source AAP accessible" in message for message in success)
    assert any("Target AAP accessible" in message for message in success)

    class FailingSourceContext:
        config = success_ctx.config

        @property
        def source_client(self):
            raise RuntimeError("source boom")

        target_client = object()

    with pytest.raises(click.ClickException, match="Source AAP connection failed"):
        config_module._test_connectivity(FailingSourceContext())

    class FailingTargetContext:
        config = success_ctx.config
        source_client = object()

        @property
        def target_client(self):
            raise RuntimeError("target boom")

    with pytest.raises(click.ClickException, match="Target AAP connection failed"):
        config_module._test_connectivity(FailingTargetContext())

    fallback_used = {"value": False}
    real_asyncio_run = py_asyncio.run

    def fake_asyncio_run(_coro):
        _coro.close()
        raise RuntimeError("event loop already running")

    class FakeLoop:
        def run_until_complete(self, coro):
            fallback_used["value"] = True
            return real_asyncio_run(coro)

    monkeypatch.setattr(config_module.asyncio, "run", fake_asyncio_run)
    monkeypatch.setattr(config_module.asyncio, "get_event_loop", lambda: FakeLoop())
    config_module._test_connectivity(success_ctx)
    assert fallback_used["value"] is True


def test_prep_command_success_and_existing_files_prompt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    client_calls: list[tuple] = []
    captures = {
        "steps": [],
        "discover": [],
        "generate": [],
        "compare": [],
        "saves": [],
        "versions": [],
        "success": [],
    }
    info_handler = RichHandler()
    info_handler.setLevel(logging.INFO)
    debug_handler = RichHandler()
    debug_handler.setLevel(logging.DEBUG)
    notset_handler = RichHandler()
    notset_handler.setLevel(logging.NOTSET)
    fake_root = SimpleNamespace(handlers=[info_handler, debug_handler, notset_handler])

    install_prep_success_patches(monkeypatch, captures)
    monkeypatch.setattr(
        prep_module,
        "logging",
        SimpleNamespace(
            getLogger=lambda: fake_root,
            INFO=logging.INFO,
            NOTSET=logging.NOTSET,
            WARNING=logging.WARNING,
            DEBUG=logging.DEBUG,
        ),
    )

    ctx = build_ctx(
        tmp_path,
        source_client=PrepClient("source", "2.4.0", client_calls),
        target_client=PrepClient("target", "2.6.0", client_calls),
    )
    output_dir = tmp_path / "schemas"

    result = runner.invoke(prep_module.prep, ["--output-dir", str(output_dir)], obj=ctx)
    assert result.exit_code == 0
    assert captures["steps"] == [
        "Connecting to source.example and target.example",
        "Detecting AAP versions",
        "Discovering endpoints",
        "Generating schemas",
        "Comparing schemas",
    ]
    assert captures["versions"] == [("2.4.0", "2.6.0")]
    assert captures["discover"] == [
        ("source", "2.4.0", ("common/", "source-only/")),
        ("target", "2.6.0", ("common/", "target-only/")),
    ]
    assert len(captures["saves"]) == 5
    assert captures["success"] == [f"Prep complete! Output: {output_dir}/"]
    assert info_handler.level == logging.WARNING
    assert debug_handler.level == logging.DEBUG
    assert notset_handler.level == logging.WARNING

    output_dir.mkdir(exist_ok=True)
    (output_dir / "source_endpoints.json").write_text("{}")
    (output_dir / "target_endpoints.json").write_text("{}")
    result = runner.invoke(
        prep_module.prep, ["--output-dir", str(output_dir)], obj=ctx, input="n\n"
    )
    assert result.exit_code == 0
    assert "Schema files already exist. Overwrite?" in result.output
    assert "Cancelled." in result.output


def test_prep_command_version_error_and_event_loop_fallback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    client_calls: list[tuple] = []
    console_messages: list[str] = []
    captures = {
        "steps": [],
        "discover": [],
        "generate": [],
        "compare": [],
        "saves": [],
        "versions": [],
        "success": [],
    }

    install_prep_success_patches(monkeypatch, captures)
    monkeypatch.setattr(
        prep_module.console, "print", lambda message: console_messages.append(str(message))
    )

    def raise_version_error(source_version: str, target_version: str):
        raise VersionValidationError("unsupported version pair")

    monkeypatch.setattr(prep_module, "validate_version_compatibility", raise_version_error)

    ctx = build_ctx(
        tmp_path,
        source_client=PrepClient("source", "2.3.0", client_calls),
        target_client=PrepClient("target", "2.6.0", client_calls),
    )
    result = runner.invoke(prep_module.prep, ["--output-dir", str(tmp_path / "schemas")], obj=ctx)
    assert result.exit_code == 1
    assert any("Version Compatibility Error" in message for message in console_messages)

    fallback_used = {"value": False}
    real_asyncio_run = py_asyncio.run

    install_prep_success_patches(monkeypatch, captures)

    def fake_asyncio_run(_coro):
        _coro.close()
        raise RuntimeError("event loop already running")

    class FakeLoop:
        def run_until_complete(self, coro):
            fallback_used["value"] = True
            return real_asyncio_run(coro)

    monkeypatch.setattr(prep_module.asyncio, "run", fake_asyncio_run)
    monkeypatch.setattr(prep_module.asyncio, "get_event_loop", lambda: FakeLoop())

    result = runner.invoke(
        prep_module.prep,
        ["--output-dir", str(tmp_path / "fallback-schemas"), "--force"],
        obj=ctx,
    )
    assert result.exit_code == 0
    assert fallback_used["value"] is True
