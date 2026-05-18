from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from aap_migration.client.exceptions import NetworkError, RateLimitError, ServerError
from aap_migration.utils import logging as logging_utils
from aap_migration.utils.retry import (
    retry_on_gateway_error,
    retry_on_network_error,
    retry_on_server_error,
    retry_with_backoff,
    retry_with_rate_limit_handling,
)
from aap_migration.utils.version_validation import (
    VersionValidationError,
    get_version_info,
    parse_version,
    validate_version_compatibility,
)


def make_status_error(status_code: int, detail: str = "boom") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com/api")
    response = httpx.Response(status_code, request=request, json={"detail": detail})
    return httpx.HTTPStatusError("bad status", request=request, response=response)


def test_retry_on_network_error_retries_sync_function() -> None:
    attempts = {"count": 0}

    @retry_on_network_error(max_attempts=3, min_wait=0, max_wait=0)
    def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise NetworkError("temporary")
        return "ok"

    assert flaky() == "ok"
    assert attempts["count"] == 3


@pytest.mark.asyncio
async def test_retry_on_server_error_retries_5xx_but_not_4xx() -> None:
    attempts = {"count": 0}

    @retry_on_server_error(max_attempts=3, min_wait=0, max_wait=0)
    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise make_status_error(503)
        return "ok"

    assert await flaky() == "ok"
    assert attempts["count"] == 3

    @retry_on_server_error(max_attempts=3, min_wait=0, max_wait=0)
    async def client_error() -> str:
        raise make_status_error(400, "bad request")

    with pytest.raises(httpx.HTTPStatusError):
        await client_error()


@pytest.mark.asyncio
async def test_retry_with_backoff_retries_custom_exceptions() -> None:
    attempts = {"count": 0}

    @retry_with_backoff(
        max_attempts=3,
        min_wait=0,
        max_wait=0,
        retry_on_exceptions=(ServerError,),
    )
    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise ServerError("retry me")
        return "done"

    assert await flaky() == "done"
    assert attempts["count"] == 2


@pytest.mark.asyncio
async def test_retry_with_rate_limit_handling_uses_retry_after_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    attempts = {"count": 0}

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def coro() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RateLimitError("limited", retry_after=7)
        if attempts["count"] == 2:
            raise RateLimitError("limited again")
        return "ok"

    monkeypatch.setattr("aap_migration.utils.retry.asyncio.sleep", fake_sleep)

    assert (
        await retry_with_rate_limit_handling(coro, max_attempts=4, min_wait=2, max_wait=9) == "ok"
    )
    assert sleeps == [7, 8]


@pytest.mark.asyncio
async def test_retry_with_rate_limit_handling_raises_after_exhaustion() -> None:
    async def always_limited() -> str:
        raise RateLimitError("still limited")

    with pytest.raises(RateLimitError):
        await retry_with_rate_limit_handling(always_limited, max_attempts=1)


@pytest.mark.asyncio
async def test_retry_on_gateway_error_retries_async_and_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async_sleeps: list[float] = []
    sync_sleeps: list[float] = []
    async_attempts = {"count": 0}
    sync_attempts = {"count": 0}

    async def fake_async_sleep(seconds: float) -> None:
        async_sleeps.append(seconds)

    def fake_time_sleep(seconds: float) -> None:
        sync_sleeps.append(seconds)

    monkeypatch.setattr("aap_migration.utils.retry.asyncio.sleep", fake_async_sleep)
    monkeypatch.setattr("time.sleep", fake_time_sleep)

    @retry_on_gateway_error(max_attempts=3, backoff_base=2.0)
    async def async_flaky() -> str:
        async_attempts["count"] += 1
        if async_attempts["count"] < 3:
            raise make_status_error(502)
        return "async-ok"

    @retry_on_gateway_error(max_attempts=2, backoff_base=3.0)
    def sync_flaky() -> str:
        sync_attempts["count"] += 1
        if sync_attempts["count"] < 2:
            raise RuntimeError("503 gateway overloaded")
        return "sync-ok"

    assert await async_flaky() == "async-ok"
    assert sync_flaky() == "sync-ok"
    assert async_sleeps == [2.0, 4.0]
    assert sync_sleeps == [3.0]


def test_logging_helpers_redact_truncate_and_gate_payloads() -> None:
    payload = {
        "token": "secret",
        "nested": [{"password": "hidden"}, {"safe": "value"}],
        "normal": "ok",
    }

    sanitized = logging_utils.sanitize_payload(payload)
    truncated = logging_utils.truncate_payload({"a": "x" * 40}, max_size=20)

    assert sanitized["token"] == "[REDACTED]"
    assert sanitized["nested"][0]["password"] == "[REDACTED]"
    assert sanitized["normal"] == "ok"
    assert logging_utils.sanitize_payload({"a": {"b": {"c": "d"}}}, max_depth=1) == {
        "a": "[MAX_DEPTH_EXCEEDED]"
    }
    assert "[TRUNCATED" in truncated

    logger = SimpleNamespace(_logger=logging.getLogger("payload-test"))
    logger._logger.setLevel(logging.DEBUG)
    assert logging_utils.should_log_payloads(logger, True) is True
    assert logging_utils.should_log_payloads(logger, False) is False
    assert logging_utils.should_log_payloads(SimpleNamespace(), True) is True


def test_json_file_formatter_and_strip_ansi_codes() -> None:
    formatter = logging_utils.JSONFileFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="\x1b[31mboom\x1b[0m",
        args=(),
        exc_info=None,
    )

    rendered = formatter.format(record)

    assert '"event": "boom"' in rendered
    assert '"logger": "test.logger"' in rendered
    assert logging_utils._strip_ansi_codes("\x1b[32mgreen\x1b[0m") == "green"
    assert logging_utils.add_app_context(None, "info", {"event": "x"})["app"] == "aap-bridge"

    try:
        raise ValueError("formatter failure")
    except ValueError:
        exc_record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failure",
            args=(),
            exc_info=sys.exc_info(),
        )

    assert '"exception":' in formatter.format(exc_record)


def test_configure_logging_and_structured_helper_branches(tmp_path: Path) -> None:
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]
    original_level = root_logger.level

    class CapturingLogger:
        def __init__(self) -> None:
            self.calls = []

        def info(self, event, **kwargs):
            self.calls.append(("info", event, kwargs))

        def warning(self, event, **kwargs):
            self.calls.append(("warning", event, kwargs))

        def error(self, event, **kwargs):
            self.calls.append(("error", event, kwargs))

    try:
        json_log = tmp_path / "logs" / "app.json"
        console_log = tmp_path / "logs" / "app.log"

        logging_utils.configure_logging(
            level="info",
            log_format="json",
            log_file=str(json_log),
            file_level="error",
            enable_colors=False,
        )
        assert root_logger.level == logging.DEBUG
        assert any(isinstance(handler, logging.StreamHandler) for handler in root_logger.handlers)
        assert any(
            isinstance(handler, logging.FileHandler)
            and isinstance(handler.formatter, logging_utils.JSONFileFormatter)
            for handler in root_logger.handlers
        )

        logging_utils.configure_logging(
            level="warning",
            log_format="console",
            log_file=str(console_log),
        )
        assert any(
            isinstance(handler, logging.FileHandler)
            and isinstance(handler.formatter, logging.Formatter)
            and not isinstance(handler.formatter, logging_utils.JSONFileFormatter)
            for handler in root_logger.handlers
        )

        logger = CapturingLogger()
        logging_utils.log_api_request(logger, "GET", "/api/v2/ping/")
        logging_utils.log_api_request(
            logger, "GET", "/api/v2/ping/", status_code=204, duration_ms=12.345
        )
        logging_utils.log_api_request(logger, "GET", "/api/v2/ping/", status_code=404)
        logging_utils.log_api_request(logger, "GET", "/api/v2/ping/", status_code=503)
        logging_utils.log_api_request(logger, "GET", "/api/v2/ping/", status_code=302)
        logging_utils.log_migration_progress(
            logger, "import", "projects", completed=3, total=0, job="abc"
        )
        logging_utils.log_checkpoint(logger, "checkpoint-1", "export", items_processed=5)
        logging_utils.log_error(logger, RuntimeError("boom"), "testing", request_id="req-1")

        assert [call[:2] for call in logger.calls[:5]] == [
            ("info", "api_request_started"),
            ("info", "api_request_success"),
            ("warning", "api_request_client_error"),
            ("info", "api_request_server_error"),
            ("info", "api_request_completed"),
        ]
        assert logger.calls[1][2]["duration_ms"] == 12.35
        assert logger.calls[5][2]["percentage"] == 0
        assert logger.calls[6][1] == "checkpoint_created"
        assert logger.calls[7][2]["error_type"] == "RuntimeError"
        assert logger.calls[7][2]["exc_info"] is True

        class NotJsonSerializable:
            def __str__(self) -> str:
                return "fallback-string"

        assert logging_utils.sanitize_payload("primitive") == "primitive"
        assert (
            logging_utils.truncate_payload(NotJsonSerializable(), max_size=100)
            == '"fallback-string"'
        )
        assert logging_utils.truncate_payload({"ok": "x"}, max_size=100).startswith("{")
    finally:
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
            close = getattr(handler, "close", None)
            if callable(close):
                close()
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)


def test_version_validation_parsing_and_metadata() -> None:
    assert parse_version("2.6.1-dev+build") == (2, 6, 1)
    assert get_version_info("2.6.0")["supports_platform_gateway"] is True

    with pytest.raises(ValueError):
        parse_version("invalid")

    validate_version_compatibility("2.4.1", "2.6.0")
    validate_version_compatibility("2.6.0", "2.5.0")

    with pytest.raises(VersionValidationError, match="below minimum supported version"):
        validate_version_compatibility("2.2.0", "2.6.0")

    with pytest.raises(VersionValidationError, match="Version parsing failed"):
        validate_version_compatibility("bad", "2.6.0")

    assert get_version_info("bad")["major"] == 0
