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

import hmac
import logging
import os
import time as _time
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from finance_agent.api_helpers import (
    _REQUEST_COUNTS,
    _REQUEST_LATENCY,
    _RESPONSE_CACHES,
    FocalUser,
    NullableFloat,
    NullableStr,
    _cached_response,
    _client_ip,
    _cors_origins,
    _facts,
    _facts_or_503,
    _jsonable,
    _request_cid,
    _SlidingWindowLimiter,
)
from finance_agent.api_helpers import (
    _RESPONSE_CACHES as _CACHE_LIST,
)
from finance_agent.observability import configure_logging, correlation, report_exception

log = logging.getLogger("finance_agent.api")

_VERSION = "0.1.0"
_STARTED_AT = _time.time()
_CONFIG_PATH = "config.yaml"


def create_app(config_path: str | None = None) -> FastAPI:
    """Build the FastAPI application (used by uvicorn, compose, and tests)."""
    import yaml

    from finance_agent.api_helpers import _CONFIG_PATH as _mod_cfg

    if config_path:
        import finance_agent.api_helpers as _helpers
        _helpers._CONFIG_PATH = config_path

    configure_logging()

    # Audit §2 (bundle-signing rotation)
    try:
        from finance_agent.bundle_security import ensure_bundle_verified

        with open(config_path or _CONFIG_PATH, encoding="utf-8") as fh:
            _boot_cfg = yaml.safe_load(fh) or {}
        _boot_bundle = str(
            _boot_cfg.get("model_bench", {}).get(
                "bundle_path", "model_bench/risk_model_bundle.joblib"
            )
        )
        if os.path.exists(_boot_bundle) and os.environ.get("FINSIGHT_BUNDLE_KEY", "").strip():
            ensure_bundle_verified(_boot_bundle)
    except (OSError, yaml.YAMLError):
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
            {"name": "health", "description": "Service health check and readiness probes"},
            {"name": "facts", "description": "Monthly summaries, category breakdowns, budgets, and financial health"},
            {"name": "risk", "description": "Per-transaction risk scoring, SHAP explanations, and similar transactions"},
            {"name": "admin", "description": "Service reload, metrics, and metadata"},
        ],
    )

    # --- OpenTelemetry distributed tracing (OTEL_ENABLED=true) ---
    try:
        from finance_agent.tracing import setup_tracing
        _otel_ok = setup_tracing("finsight-api")
        if _otel_ok:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(app)
    except ImportError:
        pass

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
        """D.1: thread a correlation id through the whole request lifecycle."""
        cid = _request_cid(request)
        with correlation(cid):
            try:
                response = await call_next(request)
                response.headers["X-Request-Id"] = cid
                return response
            except (RuntimeError, ValueError, OSError) as exc:
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
        if request.method == "OPTIONS" or not api_key or not request.url.path.startswith("/api/"):
            return await call_next(request)
        provided = request.headers.get("X-API-Key") or ""
        if hmac.compare_digest(provided, api_key):
            return await call_next(request)
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
        log.warning("Invalid FINSIGHT_RATE_LIMIT_PER_MIN value — rate limiting disabled")
        rate_per_min = 0
    limiter = _SlidingWindowLimiter(rate_per_min) if rate_per_min > 0 else None

    @app.middleware("http")
    async def _rate_limit(request: Request, call_next: Any) -> Any:
        """Audit §5: optional per-IP rate limiting over the /api/* surface."""
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
        """Audit §6: baseline security headers on *every* response."""
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
            "rows": len(facts.df),
            "rule_only": facts.rule_only(),
            "focal_user": facts.focal_user,
            "focal_users": facts.focal_users,
        }

    @app.get(
        "/metrics",
        responses={200: {"content": {"text/plain": {"schema": {"type": "string"}}}}},
    )
    def metrics() -> Response:
        """Prometheus text-format metrics (D.1)."""
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
        """Paginated ledger view (C.1.3)."""
        facts = _facts_or_503(user)
        df = facts.focal() if focal_only else facts.df
        total = len(df)
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
        """Phase B.1 — nearest transactions in feature space with fraud labels."""
        return _jsonable(
            _facts_or_503(user).find_similar_transactions(transaction_id=transaction_id, k=k)
        )

    @app.post("/api/v1/reload")
    def reload() -> dict[str, Any]:
        """Drop the cached facts snapshot AND the per-endpoint response caches."""
        _facts.cache_clear()
        for cache in _RESPONSE_CACHES:
            cache.clear()
        return {"status": "ok", "detail": "facts snapshot will be reloaded on the next request"}

    return app


# Module-level ASGI app for `uvicorn finance_agent.api:app`
app = create_app()
