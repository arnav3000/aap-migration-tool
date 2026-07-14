"""Unit tests for IAMAnalyser._paginate() pagination logic.

Tests use unittest.mock to patch requests.Session — no live server needed.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from aap_migration.iam.analyser import IAMAnalyser
from aap_migration.iam.exceptions import PaginationError


def _make_response(status_code, body):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = json.dumps(body)
    resp.json.return_value = body
    resp.url = "https://aap.example.com/api/v2/credentials/"
    return resp


def _build_analyser():
    with patch("requests.Session"):
        analyser = IAMAnalyser(
            source_url="https://aap.example.com/api/v2",
            source_token="fake-token",
            verify_ssl=False,
            rate_limit_delay=0,
        )
    return analyser


class TestPaginateRelativeNextUrls:
    """Test (a): 3 pages with root-relative next links."""

    def test_collects_all_items_across_three_pages(self):
        analyser = _build_analyser()
        page1 = _make_response(200, {
            "count": 6,
            "next": "/api/v2/credentials/?page=2&page_size=2",
            "previous": None,
            "results": [{"id": 1}, {"id": 2}],
        })
        page2 = _make_response(200, {
            "count": 6,
            "next": "/api/v2/credentials/?page=3&page_size=2",
            "previous": "/api/v2/credentials/?page_size=2",
            "results": [{"id": 3}, {"id": 4}],
        })
        page3 = _make_response(200, {
            "count": 6,
            "next": None,
            "previous": "/api/v2/credentials/?page=2&page_size=2",
            "results": [{"id": 5}, {"id": 6}],
        })

        session = analyser._source_session
        session.get.side_effect = [page1, page2, page3]

        results = analyser._source_paginate("credentials/")

        assert len(results) == 6
        assert [r["id"] for r in results] == [1, 2, 3, 4, 5, 6]

        calls = session.get.call_args_list
        assert len(calls) == 3

        page2_url = calls[1][0][0]
        assert "/api/v2/api/v2/" not in page2_url, (
            f"Doubled path detected in page 2 URL: {page2_url}"
        )
        assert page2_url == "https://aap.example.com/api/v2/credentials/?page=2&page_size=2"

        page3_url = calls[2][0][0]
        assert "/api/v2/api/v2/" not in page3_url, (
            f"Doubled path detected in page 3 URL: {page3_url}"
        )
        assert page3_url == "https://aap.example.com/api/v2/credentials/?page=3&page_size=2"


class TestPaginateAbsoluteNextUrls:
    """Test (b): absolute next URLs use _validate_next_url."""

    def test_absolute_next_url_with_matching_host(self):
        analyser = _build_analyser()
        page1 = _make_response(200, {
            "count": 4,
            "next": "https://aap.example.com/api/v2/credentials/?page=2&page_size=2",
            "previous": None,
            "results": [{"id": 1}, {"id": 2}],
        })
        page2 = _make_response(200, {
            "count": 4,
            "next": None,
            "previous": "https://aap.example.com/api/v2/credentials/?page_size=2",
            "results": [{"id": 3}, {"id": 4}],
        })

        session = analyser._source_session
        session.get.side_effect = [page1, page2]

        results = analyser._source_paginate("credentials/")

        assert len(results) == 4
        page2_url = session.get.call_args_list[1][0][0]
        assert page2_url == "https://aap.example.com/api/v2/credentials/?page=2&page_size=2"

    def test_absolute_next_url_with_mismatched_host_stops(self):
        analyser = _build_analyser()
        page1 = _make_response(200, {
            "count": 4,
            "next": "https://evil.example.com/api/v2/credentials/?page=2",
            "previous": None,
            "results": [{"id": 1}, {"id": 2}],
        })

        session = analyser._source_session
        session.get.side_effect = [page1]

        with pytest.raises(PaginationError, match="count mismatch"):
            analyser._source_paginate("credentials/")


class TestPaginateSinglePage:
    """Test (c): single page, next=null."""

    def test_single_page_no_extra_requests(self):
        analyser = _build_analyser()
        page1 = _make_response(200, {
            "count": 3,
            "next": None,
            "previous": None,
            "results": [{"id": 1}, {"id": 2}, {"id": 3}],
        })

        session = analyser._source_session
        session.get.side_effect = [page1]

        results = analyser._source_paginate("credentials/")

        assert len(results) == 3
        assert session.get.call_count == 1


class TestPaginateMidStreamError:
    """Test (d): 404 on page 2 raises PaginationError."""

    def test_404_on_page2_raises_pagination_error(self):
        analyser = _build_analyser()
        page1 = _make_response(200, {
            "count": 4,
            "next": "/api/v2/credentials/?page=2&page_size=2",
            "previous": None,
            "results": [{"id": 1}, {"id": 2}],
        })
        page2_404 = _make_response(404, {"detail": "Not found"})

        session = analyser._source_session
        session.get.side_effect = [page1, page2_404]

        with pytest.raises(PaginationError) as exc_info:
            analyser._source_paginate("credentials/")

        err = exc_info.value
        assert err.endpoint == "credentials/"
        assert err.status_code == 404
        assert err.items_collected == 2
        assert err.expected_count == 4

    def test_404_on_page1_does_not_raise(self):
        """Page 1 non-200 breaks silently (endpoint may not exist)."""
        analyser = _build_analyser()
        page1_404 = _make_response(404, {"detail": "Not found"})

        session = analyser._source_session
        session.get.side_effect = [page1_404]

        results = analyser._source_paginate("credentials/")
        assert results == []


class TestPaginateCountMismatch:
    """Test (e): server says count=450 but pages deliver 400."""

    def test_count_mismatch_raises_pagination_error(self):
        analyser = _build_analyser()
        page1 = _make_response(200, {
            "count": 450,
            "next": "/api/v2/credentials/?page=2&page_size=200",
            "previous": None,
            "results": [{"id": i} for i in range(200)],
        })
        page2 = _make_response(200, {
            "count": 450,
            "next": None,
            "previous": "/api/v2/credentials/?page_size=200",
            "results": [{"id": i} for i in range(200, 400)],
        })

        session = analyser._source_session
        session.get.side_effect = [page1, page2]

        with pytest.raises(PaginationError) as exc_info:
            analyser._source_paginate("credentials/")

        err = exc_info.value
        assert err.items_collected == 400
        assert err.expected_count == 450
        assert "count mismatch" in str(err)

    def test_count_matches_no_error(self):
        analyser = _build_analyser()
        page1 = _make_response(200, {
            "count": 3,
            "next": None,
            "previous": None,
            "results": [{"id": 1}, {"id": 2}, {"id": 3}],
        })

        session = analyser._source_session
        session.get.side_effect = [page1]

        results = analyser._source_paginate("credentials/")
        assert len(results) == 3
