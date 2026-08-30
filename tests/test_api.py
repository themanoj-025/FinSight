"""Tests for the FastAPI facts API (Phase 6 stretch #2 — HTTP wrapper).

The session-scoped ``api_server`` fixture (conftest.py) runs the real app with
uvicorn on an ephemeral port; these tests hit it over plain HTTP.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import boot_api_server

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "model_bench" / "risk_model_bundle.joblib"

requires_bundle = pytest.mark.skipif(
    not BUNDLE.exists(), reason="model bundle not trained (run `make train`)"
)


@pytest.fixture(scope="module", autouse=True)
def _ensure_data() -> None:
    """The API serves data/transactions.csv — generate it once if missing."""
    if not (ROOT / "data" / "transactions.csv").exists():
        subprocess.run(
            [sys.executable, "generate_data.py", "--config", str(ROOT / "config.yaml")],
            cwd=str(ROOT),
            check=True,
        )


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_root_info(api_server) -> None:
    status, body = _get(f"{api_server}/")
    assert status == 200
    assert body["name"] == "FinSight Agent API"
    assert body["health"] == "/api/v1/health"


def test_health(api_server) -> None:
    status, body = _get(f"{api_server}/api/v1/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["rows"] > 0
    assert isinstance(body["rule_only"], bool)


def test_meta_exposes_config_and_scoring_mode(api_server) -> None:
    status, body = _get(f"{api_server}/api/v1/meta")
    assert status == 200
    assert "risk" in body["config"]
    assert 0.0 <= body["fraud_threshold"] <= 1.0
    assert body["scoring_mode"] in ("rule_only", "blended")
    assert isinstance(body["rule_only"], bool)


def test_monthly_summary_shape(api_server) -> None:
    status, body = _get(f"{api_server}/api/v1/monthly-summary")
    assert status == 200
    assert body["summary"]
    data = body["data"]
    assert data["month"]
    assert isinstance(data["income"], float)
    assert isinstance(data["transaction_count"], int)


def test_category_breakdown_shape(api_server) -> None:
    status, body = _get(f"{api_server}/api/v1/category-breakdown")
    assert status == 200
    assert isinstance(body["data"]["rows"], list)
    for row in body["data"]["rows"]:
        assert {"category", "amount", "share"} <= set(row)
        assert 0.0 <= row["share"] <= 1.0


def test_budget_status_shape(api_server) -> None:
    status, body = _get(f"{api_server}/api/v1/budget-status")
    assert status == 200
    assert body["summary"]
    data = body["data"]
    if data["configured"]:
        for row in data["rows"]:
            assert {"category", "goal", "spent", "pct", "over"} <= set(row)
            assert isinstance(row["over"], bool)
    else:
        assert data["rows"] == []


def test_financial_health_shape(api_server) -> None:
    status, body = _get(f"{api_server}/api/v1/financial-health")
    assert status == 200
    assert 0 <= body["data"]["score"] <= 100


def test_risk_scored_shape(api_server) -> None:
    status, body = _get(f"{api_server}/api/v1/risk-scored?focal_only=true&limit=10")
    assert status == 200
    data = body["data"]
    assert 0 <= data["flagged_count"] <= data["total_scored"]
    for row in data["rows"]:
        assert isinstance(row["risk_score"], float)
        assert isinstance(row["reason"], str)


@requires_bundle
def test_risk_scored_can_include_explanations(api_server) -> None:
    status, body = _get(
        f"{api_server}/api/v1/risk-scored?include_explanations=true&limit=5&threshold=0.5"
    )
    assert status == 200
    data = body["data"]
    if not data["explanations_available"]:
        pytest.skip("no SHAP-capable bundle on this machine")
    explained = [r for r in data["rows"] if r.get("explanation")]
    assert explained
    expl = explained[0]["explanation"]
    assert expl["method"].startswith("TreeSHAP")
    assert isinstance(expl["top_features"], list)
    assert {"feature", "contribution"} <= set(expl["top_features"][0])


def test_meta_exposes_focal_users(api_server) -> None:
    status, body = _get(f"{api_server}/api/v1/meta")
    assert status == 200
    assert isinstance(body["focal_users"], list) and body["focal_users"]
    assert body["focal_user"] in body["focal_users"]


def test_user_param_switches_focal_user(api_server) -> None:
    """Multi-user: ?user=U_Maria serves Maria's per-user monthly summary."""
    status, body = _get(f"{api_server}/api/v1/monthly-summary?user=U_Maria")
    assert status == 200
    assert body["data"]["income"] > 0


def test_empty_user_param_defaults_to_focal_user(api_server) -> None:
    """`?user=` (empty) is a natural client artifact — serves the default user.

    Contract-fuzz guard (F.3): the API must not 422 on an empty `user` value.
    """
    status, body = _get(f"{api_server}/api/v1/monthly-summary?user=")
    assert status == 200
    assert body["data"]["month"]


def test_unknown_user_param_is_422(api_server) -> None:
    """An unknown ?user= id is rejected with a clear error, not a 500.

    The detail body follows the documented ``HTTPValidationError`` shape (an
    array of error objects — F.3 contract fix), so the input value is looked up
    in the serialized array rather than by substring-matching a plain string.
    """
    status, body = _get(f"{api_server}/api/v1/monthly-summary?user=U_NotARealUser")
    assert status == 422
    assert isinstance(body["detail"], list) and body["detail"]
    assert "U_NotARealUser" in json.dumps(body["detail"])


def test_transactions_payload_carries_dtypes_and_focal_filter(api_server) -> None:
    status, body = _get(f"{api_server}/api/v1/transactions?focal_only=true")
    assert status == 200
    assert body["columns"]
    assert body["dtypes"]["amount"] == "float64"
    assert body["dtypes"]["step"] == "int64"
    assert body["dtypes"]["isFraud"] == "int64"  # generator writes 0/1 ints
    assert all(row["is_focal_user"] for row in body["data"])


def test_unknown_route_is_404(api_server) -> None:
    status, _ = _get(f"{api_server}/api/v1/does-not-exist")
    assert status == 404


def test_security_headers_on_every_response(api_server) -> None:
    """Audit §6: baseline security headers are set on API responses."""
    req = urllib.request.Request(f"{api_server}/api/v1/health")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "no-referrer"


def test_risk_scored_limit_is_bounded(api_server) -> None:
    """Audit §4/§5: the expensive risk-scored endpoint rejects out-of-range limits."""
    for bad in ("0", "-5", "999999"):
        status, _ = _get(f"{api_server}/api/v1/risk-scored?limit={bad}")
        assert status == 422, f"limit={bad} must be rejected with 422"
    status, _ = _get(f"{api_server}/api/v1/risk-scored?limit=5000")
    assert status == 200


def test_rate_limiter_429_when_configured(monkeypatch) -> None:
    """Audit §5: FINSIGHT_RATE_LIMIT_PER_MIN caps /api/* per IP (429 + Retry-After)."""
    from finance_agent.api import create_app

    monkeypatch.setenv("FINSIGHT_RATE_LIMIT_PER_MIN", "3")
    base_url, server, thread = boot_api_server(create_app())
    try:
        for _ in range(3):
            status, _ = _get(f"{base_url}/api/v1/health")
            assert status == 200
        req = urllib.request.Request(f"{base_url}/api/v1/health")
        try:
            with urllib.request.urlopen(req, timeout=10):
                pytest.fail("expected 429 once the per-minute limit is exceeded")
        except urllib.error.HTTPError as exc:
            assert exc.code == 429
            assert exc.headers.get("Retry-After"), "429 must carry Retry-After"
            assert exc.headers.get("X-Request-Id"), "429 must echo X-Request-Id"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_rate_limit_env_typo_degrades_gracefully(monkeypatch) -> None:
    """A malformed FINSIGHT_RATE_LIMIT_PER_MIN must not crash the API (audit §5)."""
    from finance_agent.api import create_app

    monkeypatch.setenv("FINSIGHT_RATE_LIMIT_PER_MIN", "600/min")
    base_url, server, thread = boot_api_server(create_app())
    try:
        status, _ = _get(f"{base_url}/api/v1/health")
        assert status == 200, "misconfigured rate limit must degrade, not crash"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_cors_origins_lockable_to_explicit_origins(monkeypatch) -> None:
    """Audit §6: FINSIGHT_CORS_ORIGINS replaces the wildcard with an allow-list."""
    from finance_agent.api import create_app

    monkeypatch.setenv("FINSIGHT_CORS_ORIGINS", "https://app.example.com")
    base_url, server, thread = boot_api_server(create_app())
    try:
        req = urllib.request.Request(
            f"{base_url}/api/v1/health",
            method="OPTIONS",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.headers.get("Access-Control-Allow-Origin") == "https://app.example.com"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_api_key_gate_when_configured() -> None:
    """With FINSIGHT_API_KEY set, /api/* requires the X-API-Key header."""
    from finance_agent.api import create_app

    os.environ["FINSIGHT_API_KEY"] = "sekret"
    base_url, server, thread = boot_api_server(create_app())
    try:
        status, _ = _get(f"{base_url}/api/v1/health")
        assert status == 401, "request without the key must be rejected"
        status, body = _get(f"{base_url}/api/v1/health", headers={"X-API-Key": "sekret"})
        assert status == 200
        assert body["status"] == "ok"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        os.environ.pop("FINSIGHT_API_KEY", None)
