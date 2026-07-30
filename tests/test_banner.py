"""Tests for CLI/container/HTML banner helpers."""

from __future__ import annotations

import pytest

from aap_migration.banner import (
    CREATORS,
    TOOL_NAME,
    get_cli_banner,
    get_container_motd,
    get_html_footer,
    get_html_meta_tags,
    get_version,
    inject_html_attribution,
    print_cli_banner,
)


def test_get_version_returns_string() -> None:
    version = get_version()
    assert isinstance(version, str)
    assert version


def test_get_version_falls_back_to_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata

    def _raise(_name: str) -> str:
        raise RuntimeError("missing package")

    monkeypatch.setattr(importlib.metadata, "version", _raise)
    assert get_version() == "dev"


def test_banner_strings_include_tool_name() -> None:
    assert TOOL_NAME in get_cli_banner()
    assert "AAP Migration" in get_container_motd()
    assert TOOL_NAME in get_html_meta_tags()
    assert CREATORS in get_html_footer()


def test_inject_html_attribution_adds_meta_and_footer() -> None:
    html = "<html><head></head><body><p>Report</p></body></html>"
    enriched = inject_html_attribution(html)
    assert "generator" in enriched
    assert CREATORS in enriched
    assert enriched.endswith("</html>")


def test_inject_html_attribution_returns_original_when_incomplete() -> None:
    assert inject_html_attribution("<div>no structure</div>") == "<div>no structure</div>"
    assert inject_html_attribution("<body></body>") == "<body></body>"


def test_print_cli_banner_writes_to_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr("builtins.print", lambda msg="", **kwargs: captured.append(msg))
    print_cli_banner()
    assert captured
    assert "AAP Migration" in captured[0]
