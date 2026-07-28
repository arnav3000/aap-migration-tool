from __future__ import annotations

from pathlib import Path

from scripts.check_container_env import validate_container_env


def test_validate_container_env_reports_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "container" / ".env"
    errors = validate_container_env(missing)

    assert errors
    assert errors[0].startswith("Missing ")
    assert "cp container/.env.container container/.env" in "\n".join(errors)
    assert "AAP_TOKEN_ENCRYPTION_KEY" in "\n".join(errors)


def test_validate_container_env_reports_missing_required_vars(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nMIGRATION_STATE_DB_PATH=postgresql://aap_user:changeme@db:5432/aap_migration\n",
        encoding="utf-8",
    )

    errors = validate_container_env(env_file)

    assert any("AAP_TOKEN_ENCRYPTION_KEY is not set" in err for err in errors)


def test_validate_container_env_rejects_placeholder_encryption_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MIGRATION_STATE_DB_PATH=postgresql://aap_user:changeme@db:5432/aap_migration\n"
        'AAP_TOKEN_ENCRYPTION_KEY="change-this-to-a-long-random-secret"\n',
        encoding="utf-8",
    )

    errors = validate_container_env(env_file)

    assert any("template placeholder" in err for err in errors)


def test_validate_container_env_accepts_valid_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MIGRATION_STATE_DB_PATH=postgresql://aap_user:secret@db:5432/aap_migration\n"
        "AAP_TOKEN_ENCRYPTION_KEY=this-is-a-long-random-secret-value\n",
        encoding="utf-8",
    )

    assert validate_container_env(env_file) == []
