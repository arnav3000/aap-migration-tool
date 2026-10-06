"""Path confinement + error redaction unit tests (P2 #14).

``security.confine_path`` guards artifact downloads and IAM report
rendering; every branch (traversal, absolute-outside, symlink escape,
in-base resolve) is pinned here instead of resting on a callable
existence assertion. ``redact_backend_error`` must never echo backend
detail, even for exceptions carrying secrets.
"""

from pathlib import Path

import pytest


class TestConfinePath:
    """P2 #14: confinement branches, not just callability."""

    def test_relative_in_base_resolves(self, tmp_path: Path) -> None:
        from aap_migration.api.security import confine_path

        target = tmp_path / "reports" / "r.md"
        assert confine_path("reports/r.md", tmp_path) == target.resolve()

    def test_absolute_in_base_resolves(self, tmp_path: Path) -> None:
        from aap_migration.api.security import confine_path

        target = tmp_path / "r.md"
        target.write_text("x")
        assert confine_path(str(target), tmp_path) == target.resolve()

    def test_dotdot_traversal_rejected(self, tmp_path: Path) -> None:
        from aap_migration.api.security import confine_path

        with pytest.raises(ValueError, match="must stay under"):
            confine_path("../outside.md", tmp_path)

    def test_absolute_outside_rejected(self, tmp_path: Path) -> None:
        from aap_migration.api.security import confine_path

        with pytest.raises(ValueError, match="must stay under"):
            confine_path("/etc/hostname", tmp_path)

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        from aap_migration.api.security import confine_path

        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        link = tmp_path / "jobs" / "evil"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        with pytest.raises(ValueError, match="must stay under"):
            confine_path("evil", link.parent)

    def test_symlink_inside_allowed(self, tmp_path: Path) -> None:
        from aap_migration.api.security import confine_path

        real = tmp_path / "real.txt"
        real.write_text("x")
        link = tmp_path / "alias.txt"
        link.symlink_to(real)
        assert confine_path("alias.txt", tmp_path) == real.resolve()

    def test_label_names_caller(self, tmp_path: Path) -> None:
        from aap_migration.api.security import confine_path

        with pytest.raises(ValueError, match="json_path"):
            confine_path("../x.json", tmp_path, label="json_path")


class TestRedactBackendError:
    """P2 #14: redaction holds even for secret-carrying exceptions."""

    def test_generic_message_for_secret_error(self) -> None:
        from aap_migration.api.security import redact_backend_error

        try:
            raise RuntimeError("connect failed with token=supersecret-token")
        except RuntimeError as exc:
            message = redact_backend_error(exc)
        assert message == "Connectivity test failed (see server logs for detail)"
        assert "supersecret" not in message

    def test_generic_message_for_timeout(self) -> None:
        from aap_migration.api.security import redact_backend_error

        assert (
            redact_backend_error(TimeoutError("timed out after 30s"))
            == "Connectivity test failed (see server logs for detail)"
        )
