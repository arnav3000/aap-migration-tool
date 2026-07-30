"""Tests for combined repo coverage threshold helper."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.check_repo_coverage import (
    meets_threshold,
    minimum_covered_lines,
    parse_backend_coverage,
    parse_frontend_coverage,
)


def test_minimum_covered_lines_uses_ceiling_for_eighty_percent() -> None:
    total = 24883
    assert minimum_covered_lines(total, 80.0) == 19907
    assert not meets_threshold(19906, total, 80.0)
    assert meets_threshold(19907, total, 80.0)


def test_parse_backend_and_frontend_coverage_files(tmp_path: Path) -> None:
    backend_xml = tmp_path / "coverage.xml"
    backend_xml.write_text(
        '<?xml version="1.0" ?><coverage lines-valid="100" lines-covered="80" line-rate="0.8" />'
    )
    frontend_json = tmp_path / "coverage-summary.json"
    frontend_json.write_text(
        json.dumps({"total": {"lines": {"total": 50, "covered": 40, "skipped": 0, "pct": 80}}})
    )

    assert parse_backend_coverage(backend_xml) == (80, 100)
    assert parse_frontend_coverage(frontend_json) == (40, 50)
    assert meets_threshold(80 + 40, 100 + 50, 80.0)
