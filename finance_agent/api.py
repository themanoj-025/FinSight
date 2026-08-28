"""HTTP API — the facts layer as a versioned, OpenAPI-documented service.

The Streamlit app becomes a client of this API when `FINSIGHT_API_URL` is set
(see ``app/common.py::get_facts``); without it the app keeps using
``FinanceFacts`` directly (the offline default). Full contract: docs/technical/API.md.

Design notes:
- **Read-only by default.** Facts endpoints only; config/data writes stay in the
  app container. After regeneration the app calls ``POST /api/v1/reload`` to
  invalidate the server-side facts snapshot.
- **Demo-grade auth.** If `FINSIGHT_API_KEY` is set, every ``/api/*`` request
  must carry an ``X-API-Key`` header. A shared secret, not enterprise auth —
  see docs/KNOWN_LIMITATIONS.md.
- **Strict JSON.** Numpy/pandas scalars are converted to plain Python so every
  response is valid JSON (no ``NaN``/``Infinity`` literals).
- The API serves a **startup snapshot** of the ledger + model bundle (one
  ``FinanceFacts`` per process); regenerate data while it runs and it keeps
  serving the old ledger until reload or restart.

Run locally with ``make api`` (http://localhost:8000/docs).
"""

from __future__ import annotations

import datetime
import functools
import hmac
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
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BeforeValidator

from finance_agent.observability import configure_logging, correlation, report_exception
from finance_agent.tools import FinanceFacts

log = logging.getLogger("finance_agent.api")

_VERSION = "0.1.0"
_CONFIG_PATH = "config.yaml"


def _null_to_none(value: Any) -> Any:
    """Map the literal string ``"null"`` to ``None`` for nullable query params.

    OpenAPI 3.1 documents `X | None` params as accepting ``null``, and
    schemathesis (contract fuzzing, F.3) sends the literal string ``"null"`` —
    which Pydantic rejects for numeric/int params (``float_parsing`` 422). A
    spec-compliant client could hit the same wall, so the API accepts it.
    """
    return None if value == "null" else value


# Nullable param aliases that accept the literal string "null" (F.3 contract
# fuzz: ``user=null`` / ``threshold=null`` / ``transaction_id=null`` are
# schema-valid and must not 422 on a spec-compliant client).
NullableStr = Annotated[str | None, BeforeValidator(_null_to_none)]
NullableFloat = Annotated[float | None, BeforeValidator(_null_to_none)]
NullableInt = Annotated[int | None, BeforeValidator(_null_to_none)]


def _configured_focal_users() -> list[str]:
    """The configured focal users, read from config.yaml without loading the ledger.

    Used to document the ``user`` query param as an enum (F.3): the API only
    serves the configured focal users, so the OpenAPI contract must say so —
    otherwise contract fuzzing flags every unknown-user 422 as "API rejected
    schema-compliant request". Falls back to the classic single-user default.
    """
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


USER_ENUM: list[str] = _configured_focal_users()

# The `user` query param: an explicit enum of the configured focal users so the
# OpenAPI contract says what the API actually serves (F.3 — an any-string `user`
# would 422 on unknown users and make every such request a "schema-compliant
# request the API rejected"). Query metadata lives in the *type* (modern
# FastAPI style) — a `Query(enum=USER_ENUM, ...)` call in an argument default
# trips ruff B008 and is no longer the recommended form. Defined after
# `USER_ENUM` (module-level evaluation) and before any endpoint uses it.
FocalUser = Annotated[
    str | None,
    BeforeValidator(_null_to_none),
    Query(enum=USER_ENUM, description="Focal user"),
]


def _cors_origins() -> list[str]:
    """CORS allow-list from ``FINSIGHT_CORS_ORIGINS`` (comma-separated); ``*`` when unset.

    The permissive ``*`` is the local-dev default (audit §6) — production
    should set the real origins (see DEPLOY.md). ``allow_credentials=False``
    everywhere means ``*`` can never be combined with cookies or auth headers,
    so the wildcard stays low-risk.
    """
    raw = os.environ.get("FINSIGHT_CORS_ORIGINS", "").strip()
    if not raw:
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _request_cid(request: Request) -> str:
    """The correlation id for a request: caller-supplied ``X-Request-Id`` or a minted one.

    Shared by the correlation middleware and the short-circuiting middlewares
    (auth gate, rate limiter) that return their own responses *before* the
    correlation middleware runs — so a 401/429 still echoes ``X-Request-Id``
    and its log line carries the id (D.1).
    """
    return (request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12])[:64]


def _client_ip(request: Request) -> str:
    """Best-effort client IP: first ``X-Forwarded-For`` hop, else the socket peer.

    Behind a reverse proxy the XFF header is set by the proxy; when the API is
    directly reachable it is client-controlled and spoofable, so the rate
    limiter is a best-effort control, not a hard boundary (documented in
    docs/KNOWN_LIMITATIONS.md).
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


class _SlidingWindowLimiter:
    """In-process sliding-window rate limiter keyed by client IP — no dependencies.

    Tracks request timestamps per key and refuses a key that exceeds
    ``max_requests`` inside ``window_seconds`` until its oldest hit ages out
    (audit §5). Per-worker-process state: correct for the default
    single-worker deployment; a multi-worker or edge setup needs a shared
    store (see docs/KNOWN_LIMITATIONS.md).
    """

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
                # Bounded memory: drop keys with no hits inside the window
                # rather than growing without limit on a flood of IPs.
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > cutoff}
            self._hits[key] = hits
            return True, 0


# ---- minimal observability (D.1) ----------------------------------------------
# In-process Prometheus-style counters, exposed at /metrics in the text format
# (no prometheus-client dependency). Reset on process start — good enough for
# local scraping / SLO checks (docs/technical/SLOs.md).
_REQUEST_COUNTS: dict[str, int] = {}
_REQUEST_LATENCY: dict[str, float] = {}
_STARTED_AT: float = _time.time()

# Response caches for the read-only facts endpoints (F.5): the API serves a
# startup snapshot (KNOWN_LIMITATIONS 21), so every facts response is
# deterministic until POST /api/v1/reload. `_cached_response` memoizes the
# JSON-safe result per (params) tuple and `reload()` clears them — this is what
# makes the "cached facts endpoints" SLO (SLOs.md: p95 < 200 ms under load)
# actually measurable: under load every client is served the same snapshot
# instead of each request re-running pandas groupbys / TreeSHAP on the full
# ledger. Never cache /metrics, / or /reload (live or state-mutating).
_RESPONSE_CACHES: list[dict[tuple[Any, ...], Any]] = []


def _cached_response(func: Any) -> Any:
    """Memoize a read-only endpoint's JSON-safe result by its hashable params.

    Applies to the facts endpoints only (data can't change until reload).
    ``functools.wraps`` preserves the original signature so FastAPI still
    extracts query params from the wrapped function. Keyed on (args, kwargs)
    — every endpoint param is a hashable scalar (str / None / bool / int /
    float), so distinct requests never collide.
    """
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
    """A ``FinanceFacts`` per focal user — the API serves a startup snapshot.

    ``POST /api/v1/reload`` (or a restart) picks up changed data/model
    artifacts; see docs/KNOWN_LIMITATIONS.md section 6. Multi-user: passing a
    ``focal_user`` serves that user's per-user tools.
    """
    return FinanceFacts(_CONFIG_PATH, focal_user=focal_user)


def _facts_or_503(focal_user: str | None = None) -> FinanceFacts:
    """The cached facts for `focal_user`, validating the id against the config.

    An unknown `user` query param yields a clean 422 (naming the known users)
    instead of an opaque 500 deep inside FinanceFacts. The detail is a
    ``ValidationError``-shaped array so the body conforms to the documented
    ``HTTPValidationError`` schema (contract-fuzz guard, F.3).
    """
    try:
        # An empty `user=` query param is a natural client artifact (a templated
        # `?user={id}` with an empty id) — treat it as "no user" (default focal
        # user) rather than 422-ing on 'Unknown focal user ""'. Only a
        # genuinely unknown non-empty id is rejected (contract-fuzz guard, F.3).
        if focal_user == "":
            focal_user = None
        if focal_user is not None:
            known = _facts().focal_users  # default instance carries the configured list
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


def create_app(config_path: str | None = None) -> FastAPI:
    """Build the FastAPI application (used by uvicorn, compose, and tests).

    Note: one app per process is assumed — `config_path` sets a module-level
    default for the shared facts singleton, so creating two apps with different
    config paths in one process would interfere (fine for the demo, not a
    pattern to copy).
    """
    global _CONFIG_PATH
    if config_path:
        _CONFIG_PATH = config_path

    configure_logging()  # D.1: structured JSON logging for the whole process

    # Audit §2 (bundle-signing rotation): when FINSIGHT_BUNDLE_KEY is set
    # (production), a model bundle on disk whose signature doesn't verify
    # against that key is a deployment misconfiguration — the bundle was
    # signed with a different key than this process is configured with.
    # Refuse to boot with a loud, specific error instead of silently serving
    # rule-only scores that would mask the problem. Demo/dev mode (env key
    # unset) keeps the documented rule-only degrade; a missing bundle is not
    # an error either way (the facts layer reports rule-only).
    try:
        from finance_agent.bundle_security import ensure_bundle_verified

        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            _boot_cfg = yaml.safe_load(fh) or {}
        _boot_bundle = str(
            _boot_cfg.get("model_bench", {}).get(
                "bundle_path", "model_bench/risk_model_bundle.joblib"
            )
        )
        if os.path.exists(_boot_bundle) and os.environ.get("FINSIGHT_BUNDLE_KEY", "").strip():
            ensure_bundle_verified(_boot_bundle)
    except (OSError, yaml.YAMLError):
        # Config not readable at import time is not our failure mode here —
        # the facts snapshot surfaces it with a clear message on first use.
        pass

    app = FastAPI(
        title="FinSight Agent API",
        version=_VERSION,
        description=(
            "Read-only facts service for FinSight Agent: monthly summaries, "
            "spending breakdowns, financial health, and per-transaction risk "
            "scoring with SHAP explanations. Interactive docs at /docs."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_tags=[
            {
                "name": "health",
                "description": "Service health check and readiness probes",
            },
            {
                "name": "facts",
                "description": "Monthly summaries, category breakdowns, budgets, and financial health",
            },
            {
                "name": "risk",
                "description": "Per-transaction risk scoring, SHAP explanations, and similar transactions",
            },
            {
                "name": "admin",
                "description": "Service reload, metrics, and metadata",
            },
        ],
    )
    # The Streamlit client is server-side (no browser CORS), but the API is
    # also consumable from the browser at /docs — allow that. Origins are
    # locked down via FINSIGHT_CORS_ORIGINS in production; `*` is the local-dev
    # default only (audit §6, always with allow_credentials=False).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    @app.middleware("http")
    async def _record_metrics(request: Request, call_next: Any) -> Any:
        """Request count + latency by route for /metrics (D.1)."""
        start = _time.perf_counter()
        response = await call_next(request)
        route = request.url.path
        _REQUEST_COUNTS[route] = _REQUEST_COUNTS.get(route, 0) + 1
        _REQUEST_LATENCY[route] = _REQUEST_LATENCY.get(route, 0.0) + (_time.perf_counter() - start)
        return response

    api_key = os.environ.get("FINSIGHT_API_KEY", "").strip()

    @app.middleware("http")
    async def _correlate(request: Request, call_next: Any) -> Any:
        """D.1: thread a correlation id through the whole request lifecycle.

        Accepts a caller-supplied ``X-Request-Id`` or mints one; echoes it back
        on the response and binds it to the context so every structured log
        line emitted while handling this request carries it (grep one id to
        reconstruct the full lifecycle). Unhandled exceptions are logged as
        structured JSON with the id, reported to Sentry when a DSN is set
        (inert otherwise), and answered with a JSON 500 that still echoes the
        id — a failed request must remain traceable by grep.
        """
        cid = _request_cid(request)
        with correlation(cid):
            try:
                response = await call_next(request)
                response.headers["X-Request-Id"] = cid
                return response
            except (RuntimeError, ValueError, OSError) as exc:  # noqa: BLE001 — boundary: log + report + reply, never leak a stack
                # The correlation id must be echoed even on an unhandled 500 —
                # otherwise a failed request can't be traced by grep (D.1
                # acceptance: the header is part of the contract, not a
                # success-path nicety). Handle it here so the 500 still flows
                # through the metrics middleware and carries the id.
                log.error(
                    "unhandled request exception",
                    exc_info=True,
                    extra={"extra_fields": {"method": request.method, "path": request.url.path}},
                )
                report_exception(exc)
                return JSONResponse(
                    {"detail": "Internal server error"},
                    status_code=500,
                    headers={"X-Request-Id": cid},
                )

    @app.middleware("http")
    async def _gate(request: Request, call_next: Any) -> Any:
        # OPTIONS preflights must pass through — a browser client would otherwise
        # 401 before the actual request when a key is configured. The key is
        # compared with hmac.compare_digest (constant-time) so a timing side
        # channel can never leak the shared secret (C.1.1).
        if request.method == "OPTIONS" or not api_key or not request.url.path.startswith("/api/"):
            return await call_next(request)
        provided = request.headers.get("X-API-Key") or ""
        if hmac.compare_digest(provided, api_key):
            return await call_next(request)
        # Audit §8: auth failures are logged with request context (client IP,
        # path, correlation id) — never the key itself. The response is built
        # here (before the correlation middleware runs), so the id is minted
        # and echoed explicitly (D.1).
        cid = _request_cid(request)
        with correlation(cid):
            log.warning(
                "API auth failure",
                extra={"extra_fields": {"ip": _client_ip(request), "path": request.url.path}},
            )
        return JSONResponse(
            {"detail": "Invalid or missing X-API-Key"},
            status_code=401,
            headers={"X-Request-Id": cid},
        )

    try:
        rate_per_min = int(os.environ.get("FINSIGHT_RATE_LIMIT_PER_MIN", "0").strip() or 0)
    except ValueError:
        # A typo'd env value must degrade to "no limiter", never crash the API.
        log.warning("Invalid FINSIGHT_RATE_LIMIT_PER_MIN value — rate limiting disabled")
        rate_per_min = 0
    limiter = _SlidingWindowLimiter(rate_per_min) if rate_per_min > 0 else None

    @app.middleware("http")
    async def _rate_limit(request: Request, call_next: Any) -> Any:
        """Audit §5: optional per-IP rate limiting over the /api/* surface.

        Disabled by default so local dev / CI / load tests are unaffected;
        set ``FINSIGHT_RATE_LIMIT_PER_MIN`` in production (DEPLOY.md). Applied
        *outside* the API-key gate, so unauthenticated scanning is limited
        too; OPTIONS preflights pass through. Over-limit requests get a 429
        with ``Retry-After`` and are logged.
        """
        if (
            limiter is None
            or request.method == "OPTIONS"
            or not request.url.path.startswith("/api/")
        ):
            return await call_next(request)
        allowed, retry_after = limiter.allow(_client_ip(request))
        if not allowed:
            cid = _request_cid(request)
            with correlation(cid):
                log.warning(
                    "rate limit exceeded",
                    extra={"extra_fields": {"ip": _client_ip(request), "path": request.url.path}},
                )
            return JSONResponse(
                {"detail": "Too many requests — slow down and try again shortly."},
                status_code=429,
                headers={"Retry-After": str(retry_after), "X-Request-Id": cid},
            )
        return await call_next(request)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Any) -> Any:
        """Audit §6: baseline security headers on *every* response (incl. 401/500).

        CSP is intentionally not set here — a strict policy would break the
        Swagger UI's inline scripts/styles; set CSP at the reverse proxy (see
        docs/KNOWN_LIMITATIONS.md).
        """
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.get("/")
    def root() -> dict[str, Any]:
        return {
            "name": "FinSight Agent API",
            "version": _VERSION,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "health": "/api/v1/health",
        }

    @app.get("/api/v1/health")
    @_cached_response
    def health() -> dict[str, Any]:
        facts = _facts_or_503()
        return {
            "status": "ok",
            "version": _VERSION,
            "rows": int(len(facts.df)),
            "rule_only": facts.rule_only(),
            "focal_user": facts.focal_user,
            "focal_users": facts.focal_users,
        }

    @app.get(
        "/metrics",
        # The documented response is text/plain (Prometheus exposition format),
        # not application/json — declared so the OpenAPI contract matches what
        # the endpoint actually returns (contract-fuzz guard, F.3).
        responses={200: {"content": {"text/plain": {"schema": {"type": "string"}}}}},
    )
    def metrics() -> Response:
        """Prometheus text-format metrics (D.1) — request counts/latency by
        route, uptime, and version info. Scrape from any Prometheus, or check
        the SLOs with scripts/slo_check.py (docs/technical/SLOs.md)."""
        lines = [
            "# HELP finsight_http_requests_total HTTP requests handled by route.",
            "# TYPE finsight_http_requests_total counter",
        ]
        for route in sorted(_REQUEST_COUNTS):
            lines.append(
                f'finsight_http_requests_total{{route="{route}"}} {_REQUEST_COUNTS[route]}'
            )
        lines += [
            "# HELP finsight_http_latency_seconds_total Total request latency by route.",
            "# TYPE finsight_http_latency_seconds_total counter",
        ]
        for route in sorted(_REQUEST_LATENCY):
            lines.append(
                f'finsight_http_latency_seconds_total{{route="{route}"}} {_REQUEST_LATENCY[route]:.6f}'
            )
        lines += [
            "# HELP finsight_uptime_seconds Seconds since the API process started.",
            "# TYPE finsight_uptime_seconds gauge",
            f"finsight_uptime_seconds {_time.time() - _STARTED_AT:.1f}",
            f'finsight_info{{version="{_VERSION}"}} 1',
        ]
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.get("/api/v1/meta")
    @_cached_response
    def meta() -> dict[str, Any]:
        facts = _facts_or_503()
        cfg = facts.cfg
        return {
            "config": _jsonable(cfg),
            "rule_only": facts.rule_only(),
            "scoring_mode": "rule_only" if facts.rule_only() else "blended",
            "fraud_threshold": float(cfg.get("risk", {}).get("fraud_threshold", 0.7)),
            "focal_user": facts.focal_user,
            "focal_users": facts.focal_users,
        }

    @app.get("/api/v1/transactions")
    @_cached_response
    def transactions(
        focal_only: bool = False,
        user: FocalUser = None,
        limit: int = Query(500, ge=1, le=5000, description="Max rows per page"),
        offset: int = Query(0, ge=0, description="Row offset for paging"),
    ) -> dict[str, Any]:
        """Paginated ledger view (C.1.3): a single response is bounded to
        ``limit`` rows (default 500, max 5000) with ``total`` + ``truncated``
        so clients can page instead of pulling the whole ledger at once."""
        facts = _facts_or_503(user)
        df = facts.focal() if focal_only else facts.df
        total = int(len(df))
        page = df.iloc[offset : offset + limit]
        return {
            "columns": list(df.columns),
            "dtypes": {c: str(df[c].dtype) for c in df.columns},
            "data": _jsonable(page.to_dict(orient="records")),
            "limit": limit,
            "offset": offset,
            "total": total,
            "truncated": offset + limit < total,
        }

    @app.get("/api/v1/monthly-summary")
    @_cached_response
    def monthly_summary(
        month: NullableStr = None,
        account_type: NullableStr = None,
        user: FocalUser = None,
    ) -> dict[str, Any]:
        return _jsonable(_facts_or_503(user).monthly_summary(month, account_type=account_type))

    @app.get("/api/v1/category-breakdown")
    @_cached_response
    def category_breakdown(
        month: NullableStr = None,
        account_type: NullableStr = None,
        user: FocalUser = None,
    ) -> dict[str, Any]:
        return _jsonable(_facts_or_503(user).category_breakdown(month, account_type=account_type))

    @app.get("/api/v1/budget-status")
    @_cached_response
    def budget_status(
        month: NullableStr = None,
        account_type: NullableStr = None,
        user: FocalUser = None,
    ) -> dict[str, Any]:
        return _jsonable(_facts_or_503(user).budget_status(month, account_type=account_type))

    @app.get("/api/v1/recurring-payments")
    @_cached_response
    def recurring_payments(user: FocalUser = None) -> dict[str, Any]:
        return _jsonable(_facts_or_503(user).recurring_payments())

    @app.get("/api/v1/spend-spikes")
    @_cached_response
    def spend_spikes() -> dict[str, Any]:
        return _jsonable(_facts_or_503().spend_spikes())

    @app.get("/api/v1/financial-health")
    @_cached_response
    def financial_health(user: FocalUser = None) -> dict[str, Any]:
        return _jsonable(_facts_or_503(user).financial_health())

    @app.get("/api/v1/forecast")
    @_cached_response
    def forecast(user: FocalUser = None) -> dict[str, Any]:
        return _jsonable(_facts_or_503(user).forecast_next_month())

    @app.get("/api/v1/risk-scored")
    @_cached_response
    def risk_scored(
        # Bounded (audit §4/§5): this endpoint can be expensive (per-request
        # SHAP when include_explanations is on), so the response size is
        # capped the same way the transactions page is.
        limit: int = Query(15, ge=1, le=5000, description="Max rows to return (1-5000)"),
        threshold: NullableFloat = None,
        focal_only: bool = False,
        include_explanations: bool = False,
        account_type: NullableStr = None,
        user: FocalUser = None,
    ) -> dict[str, Any]:
        return _jsonable(
            _facts_or_503(user).risk_scored_transactions(
                limit=limit,
                threshold=threshold,
                focal_only=focal_only,
                include_explanations=include_explanations,
                account_type=account_type,
            )
        )

    @app.get("/api/v1/tips")
    @_cached_response
    def tips(user: FocalUser = None) -> dict[str, Any]:
        return _jsonable(_facts_or_503(user).top_tips())

    @app.get("/api/v1/similar-transactions")
    @_cached_response
    def similar_transactions(
        transaction_id: NullableInt = None,
        k: int = Query(5, ge=1, le=20, description="Number of neighbours (1-20)"),
        user: FocalUser = None,
    ) -> dict[str, Any]:
        """Phase B.1 — nearest transactions in feature space with fraud labels.

        ``transaction_id`` is the original ledger row position (omit to explain
        the highest-risk flagged transaction). Gated by config.yaml
        ``features.faiss_retrieval``.
        """
        return _jsonable(
            _facts_or_503(user).find_similar_transactions(transaction_id=transaction_id, k=k)
        )

    @app.post("/api/v1/reload")
    def reload() -> dict[str, Any]:
        """Drop the cached facts snapshot AND the per-endpoint response caches.

        The next request rebuilds the FinanceFacts snapshot and re-computes
        every facts response from it."""
        _facts.cache_clear()
        for cache in _RESPONSE_CACHES:
            cache.clear()
        return {"status": "ok", "detail": "facts snapshot will be reloaded on the next request"}

    return app


# Module-level ASGI app for `uvicorn finance_agent.api:app` (E.1 deployment
# path used by `make api`, docker-compose.yml, docker-entrypoint.sh, and
# fly.toml). Deliberately a real instantiation — not a lazy proxy — so uvicorn
# imports exactly the app the tests exercise via create_app().
app = create_app()
