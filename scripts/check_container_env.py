#!/usr/bin/env python3
"""Validate container/.env before starting the compose stack."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = REPO_ROOT / "container" / ".env"
DEFAULT_TEMPLATE_PATH = REPO_ROOT / "container" / ".env.container"

REQUIRED_VARS = (
    "MIGRATION_STATE_DB_PATH",
    "AAP_TOKEN_ENCRYPTION_KEY",
)

# Template placeholders that must be replaced before `make up`.
_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "change-this-to-a-long-random-secret",
        "<your-encryption-key>",
    }
)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from a dotenv-style file."""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def validate_container_env(env_path: Path = DEFAULT_ENV_PATH) -> list[str]:
    """Return human-readable validation errors (empty list means OK)."""
    errors: list[str] = []

    if not env_path.is_file():
        display = _display_path(env_path)
        errors.append(f"Missing {display}.")
        errors.append("")
        errors.append("The web stack reads container/.env (not the repo-root .env file).")
        errors.append("Create it from the template:")
        errors.append(
            f"  cp {DEFAULT_TEMPLATE_PATH.relative_to(REPO_ROOT)} {DEFAULT_ENV_PATH.relative_to(REPO_ROOT)}"
        )
        errors.append("")
        errors.append("Then edit container/.env and set at least:")
        errors.append("  - MIGRATION_STATE_DB_PATH")
        errors.append("  - AAP_TOKEN_ENCRYPTION_KEY (long random secret)")
        return errors

    display = _display_path(env_path)
    values = _parse_env_file(env_path)

    for var in REQUIRED_VARS:
        if var not in values:
            errors.append(f"{var} is not set in {display}.")
            continue
        if not values[var].strip():
            errors.append(f"{var} is empty in {display}.")

    encryption_key = values.get("AAP_TOKEN_ENCRYPTION_KEY", "").strip()
    if encryption_key and encryption_key in _PLACEHOLDER_VALUES:
        errors.append(
            "AAP_TOKEN_ENCRYPTION_KEY is still set to the template placeholder. "
            "Set it to a long random secret before running make up."
        )
    elif encryption_key and len(encryption_key) < 16:
        errors.append(
            "AAP_TOKEN_ENCRYPTION_KEY is too short. Use a long random secret (at least 16 characters)."
        )

    db_path = values.get("MIGRATION_STATE_DB_PATH", "").strip()
    if db_path and not db_path.startswith("postgresql://"):
        errors.append(
            "MIGRATION_STATE_DB_PATH must start with postgresql:// "
            f"(got: {db_path!r}). SQLite is not supported for container deployments."
        )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help="Path to container env file (default: container/.env)",
    )
    args = parser.parse_args(argv)

    errors = validate_container_env(args.env_file)
    if not errors:
        return 0

    print("ERROR: container environment is not ready for make up.", file=sys.stderr)
    print(file=sys.stderr)
    for line in errors:
        print(line, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
