"""api_helpers.py — Shared helpers, rate limiter, caching, and facts accessors.

Extracted from api.py to keep the route module focused on endpoint definitions.
"""

from __future__ import annotations

import datetime
import functools
import logging
import math
import os
import threading
import time as _time
import uuid
from functools import lru_cache
from typing import Annotated, Any

import numpy as np
import pandas as pd
import yaml
from fastapi import HTTPException, Query, Request
from pydantic import BeforeValidator

from finance_agent.tools import FinanceFacts

log = logging.getLogger("finance_agent.api")


def _null_to_none(value: Any) -> Any:
    """Map the literal string ``"null"`` to ``None`` for nullable query params."""
    return None if value == "null" else value


# Nullable param aliases that accept the literal string "null" (F.3 contract fuzz).
NullableStr = Annotated[str | None, BeforeValidator(_null_to_none)]
NullableFloat = Annotated[float | None, BeforeValidator(_null_to_none)]
NullableInt = Annotated[int | None, BeforeValidator(_null_to_none)]


def _configured_focal_users() -> list[str]:
    """The configured focal users, read from config.yaml without loading the ledger."""
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        users = [str(u) for u in (cfg or {}).get("data", {}).get("focal_users") or []]
        if users:
            return users
        default = (cfg or {}).get("data", {}).get("focal_user")
        return [str(default)] if default else ["U_Alex"]
    except OSError:
        return ["U_Alex"]


# Module-level config path (set by create_app in api.py)
_CONFIG_PATH = "config.yaml"

USER_ENUM: list[str] = _configured_focal_users()

FocalUser = Annotated[
    str | None,
    BeforeValidator(_null_to_none),
    Query(enum=USER_ENUM, description="Focal user"),
]


def _cors_origins() -> list[str]:
    """CORS allow-list from ``FINSIGHT_CORS_ORIGINS`` (comma-separated); ``*`` when unset."""
    raw = os.environ.get("FINSIGHT_CORS_ORIGINS", "").strip()
    if not raw:
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _request_cid(request: Request) -> str:
    """The correlation id for a request: caller-supplied ``X-Request-Id`` or a minted one."""
    return (request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12])[:64]


def _client_ip(request: Request) -> str:
    """Best-effort client IP: first ``X-Forwarded-For`` hop, else the socket peer."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


class _SlidingWindowLimiter:
    """In-process sliding-window rate limiter keyed by client IP."""

    _MAX_KEYS = 10_000

    def __init__(self, max_requests: int, window_seconds: int = 60) -> None:
        self.max_requests = int(max_requests)
        self.window_seconds = int(window_seconds)
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """``(allowed, retry_after_seconds)`` for one request from ``key``."""
        now = _time.time()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if t > cutoff]
            if len(hits) >= self.max_requests:
                retry_after = max(1, int(self.window_seconds - (now - hits[0])) + 1)
                return False, retry_after
            hits.append(now)
            if len(self._hits) >= self._MAX_KEYS:
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > cutoff}
            self._hits[key] = hits
            return True, 0


# ---- minimal observability (D.1) ----------------------------------------------
_REQUEST_COUNTS: dict[str, int] = {}
_REQUEST_LATENCY: dict[str, float] = {}
_STARTED_AT: float = _time.time()

# Response caches for the read-only facts endpoints (F.5)
_RESPONSE_CACHES: list[dict[tuple[Any, ...], Any]] = []


def _cached_response(func: Any) -> Any:
    """Memoize a read-only endpoint's JSON-safe result by its hashable params."""
    cache: dict[tuple[Any, ...], Any] = {}
    _RESPONSE_CACHES.append(cache)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        key = (args, tuple(sorted(kwargs.items())))
        if key not in cache:
            cache[key] = func(*args, **kwargs)
        return cache[key]

    return wrapper


def _jsonable(obj: Any) -> Any:
    """Recursively convert numpy/pandas scalars to strict JSON-safe Python."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _jsonable(obj.tolist())
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        f = float(obj)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    return obj


@lru_cache(maxsize=8)
def _facts(focal_user: str | None = None) -> FinanceFacts:
    """A ``FinanceFacts`` per focal user — the API serves a startup snapshot."""
    return FinanceFacts(_CONFIG_PATH, focal_user=focal_user)


def _facts_or_503(focal_user: str | None = None) -> FinanceFacts:
    """The cached facts for `focal_user`, validating the id against the config."""
    try:
        if focal_user == "":
            focal_user = None
        if focal_user is not None:
            known = _facts().focal_users
            if focal_user not in known:
                raise HTTPException(
                    status_code=422,
                    detail=[
                        {
                            "type": "value_error",
                            "loc": ["query", "user"],
                            "msg": (
                                f"Unknown focal user {focal_user!r}; "
                                f"known users: {', '.join(known)}"
                            ),
                            "input": focal_user,
                        }
                    ],
                )
        return _facts(focal_user)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Data artifacts missing — run `make data` (and `make train`) "
                f"before starting the API: {exc}"
            ),
        ) from exc


def clear_caches() -> None:
    """Clear all response caches (called by reload endpoint)."""
    for cache in _RESPONSE_CACHES:
        cache.clear()
    _facts.cache_clear()
