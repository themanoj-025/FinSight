"""End-to-end tests for the app's HTTP client (ApiClient, Phase 6 stretch #2).

The key property: the ApiClient must be interchangeable with ``FinanceFacts``
from the pages' point of view — same dicts, same DataFrame dtypes — so the app

import pytest

pytestmark = pytest.mark.integration
can switch data sources via ``FINSIGHT_API_URL`` without page changes.
"""

import math
import os
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from app.api_client import ApiClient, ApiClientError
from finance_agent.tools import FinanceFacts

ROOT = Path(__file__).resolve().parent.parent
CONFIG = str(ROOT / "config.yaml")


def _json_norm(obj: Any) -> Any:
    """NaN → None, so pandas output (NaN) compares equal to strict JSON (null)."""
    if isinstance(obj, dict):
        return {k: _json_norm(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_norm(v) for v in obj]
    if isinstance(obj, float) and math.isnan(obj):
        return None
    return obj


@pytest.fixture(scope="module", autouse=True)
def _ensure_data() -> None:
    if not (ROOT / "data" / "transactions.csv").exists():
        import subprocess
        import sys

        subprocess.run(
            [sys.executable, "generate_data.py", "--config", CONFIG],
            cwd=str(ROOT),
            check=True,
        )


def _direct() -> FinanceFacts:
    return FinanceFacts(CONFIG)


def test_client_monthly_summary_matches_direct(api_server) -> None:
    client = ApiClient(api_server)
    assert client.monthly_summary() == _json_norm(_direct().monthly_summary())


def test_client_health_tools_match_direct(api_server) -> None:
    client = ApiClient(api_server)
    direct = _direct()
    assert client.financial_health() == _json_norm(direct.financial_health())
    assert client.category_breakdown() == _json_norm(direct.category_breakdown())
    assert client.budget_status() == _json_norm(direct.budget_status())
    assert client.forecast_next_month() == _json_norm(direct.forecast_next_month())
    assert client.top_tips() == _json_norm(direct.top_tips())


def test_client_risk_scored_matches_direct_with_explanations(api_server) -> None:
    client = ApiClient(api_server)
    direct = _direct()
    kwargs = {"limit": 5, "threshold": 0.5, "focal_only": True, "include_explanations": True}
    expected = _json_norm(direct.risk_scored_transactions(**kwargs))
    assert client.risk_scored_transactions(**kwargs) == expected


def test_client_rule_only_matches_direct(api_server) -> None:
    assert ApiClient(api_server).rule_only() == _direct().rule_only()


def test_client_df_dtypes_and_values_roundtrip(api_server) -> None:
    client = ApiClient(api_server)
    df, ref = client.df, _direct().df
    assert list(df.columns) == list(ref.columns)
    assert len(df) == len(ref)
    assert str(df["step"].dtype) == "int64"
    assert str(df["amount"].dtype) == "float64"
    assert str(df["isFraud"].dtype) == "int64"  # generator writes 0/1 ints
    assert str(df["is_focal_user"].dtype) == "bool"
    for col in ("amount", "isFraud", "is_focal_user", "merchant", "type"):
        assert (df[col] == ref[col]).all(), f"column {col} diverged over the wire"


def test_client_focal_frame(api_server) -> None:
    client = ApiClient(api_server)
    focal = client._focal()
    assert bool(focal["is_focal_user"].all())
    assert len(focal) <= len(client.df)


def test_client_cfg_mirrors_config(api_server) -> None:
    cfg = ApiClient(api_server).cfg
    assert cfg == _direct().cfg


def test_client_error_on_unknown_route(api_server) -> None:
    with pytest.raises(ApiClientError):
        ApiClient(api_server)._request("/api/v1/does-not-exist")


def test_client_unreachable_server_raises_clear_error() -> None:
    with pytest.raises(ApiClientError, match="unreachable"):
        ApiClient("http://127.0.0.1:1", timeout=2).ping()


def test_app_renders_against_api_when_configured(api_server) -> None:
    """The Streamlit app becomes a client of the API when FINSIGHT_API_URL is set.

    Renders the Settings page (which reads facts.df + facts.cfg) headlessly with
    the env var pointing at a live server — the data must come over HTTP.
    """
    from streamlit.testing.v1 import AppTest

    with mock.patch.dict(os.environ, {"FINSIGHT_API_URL": api_server}, clear=False):
        at = AppTest.from_file(str(ROOT / "app/pages/6_Settings.py"), default_timeout=120)
        at.run()
        assert not at.exception, f"Settings page raised against the API: {at.exception}"
