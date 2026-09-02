"""Tests for Phase D.1 structured observability.

Covers the three acceptance criteria:
1. every log line is valid JSON carrying the correlation id;
2. the API middleware threads/echoes a request id (one grep reconstructs a
   request's full lifecycle);
3. Sentry wiring is present but inert without SENTRY_DSN — nothing raises.
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request

import pytest
from conftest import boot_api_server

pytestmark = pytest.mark.integration
from finance_agent.observability import (
    CorrelationFilter,
    JsonFormatter,
    configure_logging,
    correlation,
    get_correlation_id,
    report_exception,
    set_correlation_id,
)

configure_logging()


def _record(levelno: int = logging.INFO, msg: str = "hello") -> logging.LogRecord:
    rec = logging.LogRecord("test.logger", levelno, __file__, 1, msg, None, None)
    # Mirror the real pipeline: the CorrelationFilter attaches the current id.
    CorrelationFilter().filter(rec)
    JsonFormatter().format(rec)
    return rec


class TestJsonFormatter:
    def test_emits_valid_json_with_correlation_id(self) -> None:
        set_correlation_id("req-abc123")
        try:
            rec = _record()
            payload = json.loads(JsonFormatter().format(rec))
            assert payload["msg"] == "hello"
            assert payload["level"] == "INFO"
            assert payload["logger"] == "test.logger"
            assert payload["correlation_id"] == "req-abc123"
            assert "ts" in payload
        finally:
            set_correlation_id(None)

    def test_correlation_id_is_none_outside_any_scope(self) -> None:
        rec = _record()
        payload = json.loads(JsonFormatter().format(rec))
        assert payload["correlation_id"] is None

    def test_includes_exception_traceback(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            rec = logging.LogRecord(
                "t", logging.ERROR, __file__, 1, "failed", None, exc_info=sys.exc_info()
            )
        payload = json.loads(JsonFormatter().format(rec))
        assert "boom" in payload["exc"]
        assert payload["level"] == "ERROR"

    def test_merges_extra_fields(self) -> None:
        rec = logging.LogRecord("t", logging.INFO, __file__, 1, "x", None, None)
        rec.extra_fields = {"tool": "find_similar_transactions", "latency_ms": 3}
        payload = json.loads(JsonFormatter().format(rec))
        assert payload["tool"] == "find_similar_transactions"
        assert payload["latency_ms"] == 3


class TestCorrelationScoping:
    def test_context_manager_sets_and_resets(self) -> None:
        assert get_correlation_id() is None
        with correlation("sess-1"):
            assert get_correlation_id() == "sess-1"
        assert get_correlation_id() is None

    def test_set_correlation_id_binds_for_current_task(self) -> None:
        set_correlation_id("sess-2")
        try:
            assert get_correlation_id() == "sess-2"
        finally:
            set_correlation_id(None)


class TestSentryInertness:
    def test_report_exception_inert_without_dsn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        # Must be a pure no-op: returns False and never raises, even with a
        # missing sentry_sdk (not a project dependency).
        assert report_exception(RuntimeError("x")) is False

    def test_report_exception_swallows_broken_sentry_init(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a DSN set but a broken/unavailable SDK, must return False quietly.

        Uses a sys.modules stub so the test is deterministic whether or not
        sentry_sdk happens to be installed in the environment — no network,
        no side effects on the root logger's handlers.
        """
        monkeypatch.setenv("SENTRY_DSN", "https://bogus@example.invalid/1")

        class _BrokenSentry:
            def init(self, **kwargs) -> None:
                raise RuntimeError("sentry init failed")

        monkeypatch.setitem(sys.modules, "sentry_sdk", _BrokenSentry())
        assert report_exception(RuntimeError("x")) is False
        monkeypatch.delenv("SENTRY_DSN", raising=False)


@pytest.fixture(scope="module")
def _api_server() -> None:
    """A real uvicorn server with a deliberately-crashing test route.

    Uses the project's established boot_api_server pattern (plain HTTP over
    urllib, no starlette.testclient — which newer starlette makes depend on
    the optional httpx2 package and would break CI collection).
    """
    from finance_agent.api import create_app

    app = create_app()

    @app.get("/boom")  # type: ignore[no-redef]  # dynamic test route
    def _boom() -> None:
        raise RuntimeError("kaboom-test")

    base_url, server, thread = boot_api_server(app)
    yield base_url
    server.should_exit = True
    thread.join(timeout=10)


def _get(base_url: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(base_url + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            # RFC 9110: header names are case-insensitive. Starlette normalizes
            # to lowercase on the wire (``x-request-id``), so normalize keys to
            # lowercase before comparing — a case-sensitive dict.get("X-...")
            # would spuriously fail against a perfectly correct response.
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}


class TestApiMiddleware:
    def test_echoes_caller_supplied_request_id(self, _api_server: str) -> None:
        status, headers = _get(_api_server, "/", {"X-Request-Id": "my-trace-42"})
        assert status == 200
        assert headers.get("x-request-id") == "my-trace-42"

    def test_mints_request_id_when_absent(self, _api_server: str) -> None:
        status, headers = _get(_api_server, "/")
        assert status == 200
        cid = headers.get("x-request-id")
        assert cid and len(cid) == 12  # uuid4().hex[:12]

    def test_unhandled_exception_is_logged_with_correlation_id(
        self, _api_server: str, caplog
    ) -> None:
        status, headers = _get(_api_server, "/boom", {"X-Request-Id": "trace-boom"})
        assert status == 500
        assert headers.get("x-request-id") == "trace-boom"

        # The middleware must have logged the exception carrying the request's
        # correlation id (server runs in-process in a thread, so caplog sees it).
        records = [
            r
            for r in caplog.records
            if r.name.startswith("finance_agent.api")
            and "unhandled request exception" in r.getMessage()
        ]
        assert records, "expected a structured log record for the unhandled exception"
        assert records[-1].correlation_id == "trace-boom"
