"""P2 #19: single parse_organizations home + legacy spellings."""

from typing import Any

import pytest
from fastapi.testclient import TestClient


def test_parse_organizations_canonical() -> None:
    from aap_migration.api.services._core import parse_organizations

    assert parse_organizations({}) is None
    assert parse_organizations({"orgs": None}) is None
    assert parse_organizations({"orgs": ""}) is None
    assert parse_organizations({"orgs": "A, B"}) == ["A", "B"]
    assert parse_organizations({"organizations": ["X"]}) == ["X"]
    assert parse_organizations({"organization": "Solo"}) == ["Solo"]
    assert parse_organizations({"organizations": []}) == []


def test_parse_organizations_ambiguity_rejected() -> None:
    from aap_migration.api.services._core import parse_organizations

    with pytest.raises(ValueError, match="Ambiguous"):
        parse_organizations({"organizations": ["A"], "organization": "B"})
    with pytest.raises(ValueError, match="Ambiguous"):
        parse_organizations({"orgs": "A", "organization": "B"})


def test_wrong_spelling_does_not_silently_widen() -> None:
    from aap_migration.api.services._core import parse_organizations

    # Typo with values must fail closed, never widen to None (= all).
    with pytest.raises(ValueError, match="Unknown organization field"):
        parse_organizations({"organisations": ["A"]})
    with pytest.raises(ValueError, match="Unknown organization field"):
        parse_organizations({"org": "A"})


def test_workers_share_helper() -> None:
    import inspect

    import aap_migration.api.services.reporting as rep

    src = (
        inspect.getsource(rep.run_validate)
        + inspect.getsource(rep.run_analyze_dependencies)
        + inspect.getsource(rep.run_enhanced_report)
    )
    assert src.count("parse_organizations") >= 3


def test_schemas_accept_legacy_spellings(pair: Any, client: TestClient, monkeypatch: Any) -> None:
    from api_shared import _fake_success

    # Analyze accepts singular/legacy spellings (previously 422 extra=forbid).
    _fake_success(monkeypatch, "run_analyze_dependencies")
    r1 = client.post("/api/v1/analysis/dependencies", json={"organization": "Default"})
    assert r1.status_code == 202, r1.text
    # Validate accepts list spelling.
    _fake_success(monkeypatch, "run_validate")
    r2 = client.post("/api/v1/validations", json={"organizations": ["Default"]})
    assert r2.status_code == 202, r2.text
    # Enhanced accepts plural spelling.
    _fake_success(monkeypatch, "run_enhanced_report")
    r3 = client.post("/api/v1/reports/enhanced", json={"organizations": ["Default"]})
    assert r3.status_code == 202, r3.text
