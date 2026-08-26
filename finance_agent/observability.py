"""Structured observability (D.1) — JSON logging, correlation ids, inert Sentry.

Design goals, kept deliberately dependency-light (stdlib ``logging`` + a
ContextVar only):

- **Every log line is valid JSON** — a single ``JsonFormatter`` installed on the
  root logger means *all* existing ``log = logging.getLogger("finance_agent.*")``
  call sites across agent.py / api.py / digest.py / tools.py / retrieval.py /
  alerts.py become structured with zero per-module changes.
- **Correlation ids** — a ``ContextVar`` is threaded per API request (middleware,
  echoed as ``X-Request-Id``) and per Streamlit session (``run_render``); every
  record in that scope carries the id, so grepping one id reconstructs a
  request/session's full lifecycle.
- **Sentry wiring that is inert without a DSN** — ``report_exception`` only
  imports ``sentry_sdk`` (not a dependency) when ``SENTRY_DSN`` is set; without
  it, it is a guaranteed no-op (asserted by tests/test_observability.py).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_CORRELATION_ID: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_configured = False
_sentry_initialized = False


class CorrelationFilter(logging.Filter):
    """Attach the current correlation id to every record passing through."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _CORRELATION_ID.get()
        return True


class JsonFormatter(logging.Formatter):
    """Single-line JSON formatter: ts / level / logger / msg / correlation_id.

    Callers can attach extra fields via ``log.info("...", extra={"k": v})`` —
    they are merged into the emitted object (exception tracebacks included as
    ``exc``). Non-serializable extras are stringified rather than dropped.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", None),
        }
        exc_info = record.exc_info if isinstance(record.exc_info, tuple) else None
        if exc_info:
            payload["exc"] = self.formatException(exc_info)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter + correlation filter on the root logger.

    Idempotent: a second call (e.g. every ``create_app`` / page render in the
    same process) does not stack duplicate handlers.
    """
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationFilter())
    root.addHandler(handler)
    root.addFilter(CorrelationFilter())
    root.setLevel(level)
    _configured = True


def get_correlation_id() -> str | None:
    return _CORRELATION_ID.get()


@contextmanager
def correlation(correlation_id: str | None) -> Any:
    """Run a block with `correlation_id` bound to the context (request scope)."""
    token = _CORRELATION_ID.set(correlation_id)
    try:
        yield
    finally:
        _CORRELATION_ID.reset(token)


def set_correlation_id(correlation_id: str | None) -> None:
    """Bind `correlation_id` for the current task/session (Streamlit scope)."""
    _CORRELATION_ID.set(correlation_id)


def report_exception(exc: BaseException) -> bool:
    """Report `exc` to Sentry — only when a DSN exists and sentry is installed.

    Guaranteed inert without ``SENTRY_DSN`` (asserted by test): the lazy import
    and init never run, so nothing can raise. Returns True when the report was
    actually dispatched.
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    global _sentry_initialized
    try:
        import sentry_sdk  # type: ignore[import-not-found]

        if not _sentry_initialized:
            sentry_sdk.init(
                dsn=dsn,
                traces_sample_rate=0.0,
                environment=os.environ.get("ENVIRONMENT", "dev"),
            )
            _sentry_initialized = True
        sentry_sdk.capture_exception(exc)
        return True
    except Exception:  # noqa: BLE001 — observability must never break the app
        return False
