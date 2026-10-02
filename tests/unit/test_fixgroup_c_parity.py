"""W3 (low-risk): schema-vs-Click-option parity for three CLI commands.

Workers duplicate CLI logic instead of the call_command allowlist
(_core._service_command_registry); risky rewrites are out of scope.
This test pins that request schemas cover the CLI flags for
analyze-dependencies / prep / validate (output paths exempt:
server-managed workdir).
"""

from typing import Any


def _click_param_names(cmd: Any) -> set[str]:
    return {p.name for p in getattr(cmd, "params", [])}


def test_analyze_dependencies_parity() -> None:
    from aap_migration.api.schemas import AnalyzeDependenciesRequest
    from aap_migration.cli.commands.analyze_dependencies import analyze_dependencies_cmd

    cli = _click_param_names(analyze_dependencies_cmd)
    # Scope flags must be covered; output/format flags are server-managed.
    assert "organization" in cli
    assert "analyze_all" in cli
    assert "verbose" in cli
    fields = set(AnalyzeDependenciesRequest.model_fields)
    assert "organizations" in fields or "organization" in fields
    assert "analyze_all" in fields
    assert "verbose" in fields
    # Legacy spellings covered at the boundary (single home).
    assert "organization" in fields
    assert "orgs" in fields


def test_prep_parity() -> None:
    from aap_migration.api.schemas import PrepRequest
    from aap_migration.cli.commands.prep import prep

    cli = _click_param_names(prep)
    assert "force" in cli
    # --output-dir is server-managed (workdir/schemas); API omits it by design.
    assert "output_dir" in cli
    fields = set(PrepRequest.model_fields)
    assert "force" in fields


def test_validate_parity() -> None:
    from aap_migration.api.schemas import ValidateRequest
    from aap_migration.cli.commands.validate import validate

    cli = _click_param_names(validate)
    for flag in ("live", "resource_type", "skip_hosts", "orgs"):
        assert flag in cli, f"CLI missing {flag}"
    fields = set(ValidateRequest.model_fields)
    for flag in ("live", "resource_type", "skip_hosts", "orgs"):
        assert flag in fields, f"schema missing {flag}"
    # Legacy org spellings share the single helper.
    assert "organizations" in fields and "organization" in fields
