"""Fixgroup A: console scrub covers password/secret-shaped values (P0 #1).

Every persist/serve console path (``_persist_console`` / ``_console_tail`` /
``read_console_tail``) scrubs through ``_scrub_output``; credential/vault
output echoing ``password=``/``secret:``-shaped values must not leak via
``GET /jobs/{id}/console``.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from aap_migration.api.jobs._scrub import _scrub_output


@pytest.mark.parametrize(
    "raw,leaked",
    [
        ("db password=hunter2", "hunter2"),
        ("login with Password: s3cr3t!", "s3cr3t!"),
        ("vault_password=supersecret", "supersecret"),
        ("ansible secret: myvalue", "myvalue"),
        ("api_key=AKIA123456", "AKIA123456"),
        ("ssh_key_data=AAAAE2VzdGVzdA==", "AAAAE2VzdGVzdA=="),
        ("private_key=-----BEGIN-FAKE-----", "-----BEGIN-FAKE-----"),
        ("passwd: x9y8z", "x9y8z"),
        ("pwd=mine", "mine"),
        ('{"password": "hunter2"}', "hunter2"),
        ("secret=s3cr3t rest of line", "s3cr3t"),
        ("VAULT_PASSWORD : hunter2", "hunter2"),
    ],
)
def test_password_secret_shapes_redacted(raw: str, leaked: str) -> None:
    out = _scrub_output(raw)
    assert leaked not in out, f"leaked value {leaked!r} in {out!r}"
    assert "***" in out


def test_preexisting_shapes_still_redacted() -> None:
    assert _scrub_output("Authorization: Bearer abc.def.ghi") == "Authorization: Bearer ***"
    assert "hunter2" not in _scrub_output("token=hunter2")
    assert "AKIA1" not in _scrub_output("x-api-key: AKIA1")
    out = _scrub_output("https://user:hunter2@host/api")
    assert "hunter2" not in out and "://***@" in out


def test_console_serve_path_redacts(tmp_path: Any) -> None:
    """The read path behind GET /jobs/{id}/console redacts secrets."""
    from pathlib import Path

    from aap_migration.api.jobs._console import _console_tail, read_console_tail

    job_dir = Path(str(tmp_path)) / "job"
    job_dir.mkdir()
    (job_dir / "console.log").write_text("starting\nvault_password=hunter2\ndone\n")
    assert "hunter2" not in read_console_tail(str(job_dir))
    assert "hunter2" not in _console_tail("ok\npassword=hunter2")


def test_scrub_never_raises() -> None:
    assert _scrub_output("") == ""
    assert _scrub_output("plain line, no secrets") == "plain line, no secrets"
    # Non-string / pathological input must not raise (returns input as-is).
    assert _scrub_output(cast(Any, None)) is None
    assert _scrub_output(cast(Any, 123)) == 123
