"""CLI tests for IAM commands (error paths and report regeneration)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from aap_migration.cli.commands.iam import iam


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_iam_audit_requires_source_env(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOURCE__URL", raising=False)
    monkeypatch.delenv("SOURCE__TOKEN", raising=False)
    result = runner.invoke(iam, ["audit"])
    assert result.exit_code == 1
    assert "SOURCE__URL" in result.output


def test_iam_benchmark_requires_source_env(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOURCE__URL", raising=False)
    monkeypatch.delenv("SOURCE__TOKEN", raising=False)
    result = runner.invoke(iam, ["benchmark"])
    assert result.exit_code == 1
    assert "SOURCE__URL" in result.output


def test_iam_migrate_rejects_conflicting_flags(runner: CliRunner) -> None:
    result = runner.invoke(
        iam,
        ["migrate", "--skip-user-roles", "--users-only"],
    )
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


def test_iam_migrate_requires_source_env(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOURCE__URL", raising=False)
    monkeypatch.delenv("SOURCE__TOKEN", raising=False)
    monkeypatch.setenv("TARGET__URL", "https://target.example.com")
    monkeypatch.setenv("TARGET__TOKEN", "dst-token")
    result = runner.invoke(iam, ["migrate"])
    assert result.exit_code == 1
    assert "SOURCE__URL" in result.output


def test_iam_migrate_requires_target_env(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOURCE__URL", "https://source.example.com")
    monkeypatch.setenv("SOURCE__TOKEN", "src-token")
    monkeypatch.delenv("TARGET__URL", raising=False)
    monkeypatch.delenv("TARGET__TOKEN", raising=False)
    result = runner.invoke(iam, ["migrate"])
    assert result.exit_code == 1
    assert "TARGET__URL" in result.output


def test_iam_report_regenerates_html(runner: CliRunner, tmp_path: Path) -> None:
    json_path = tmp_path / "iam_audit.json"
    payload = {
        "metadata": {"mode": "audit", "source_url": "https://aap.example.com"},
        "statistics": {"permissions_found": 1},
        "permissions": [
            {
                "resource_type": "projects",
                "resource_id": 1,
                "resource_name": "Demo",
                "resource_org": "Default",
                "role_name": "use",
                "principal_type": "user",
                "principal_id": 1,
                "principal_name": "alice",
                "principal_org": "Default",
            }
        ],
    }
    json_path.write_text(json.dumps(payload))

    result = runner.invoke(iam, ["report", str(json_path)])
    assert result.exit_code == 0
    html_path = tmp_path / "iam_audit.html"
    assert html_path.exists()
    assert "HTML report generated" in result.output
