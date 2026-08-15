"""Smoke tests — the app imports AND actually renders headlessly.

Page-render tests use Streamlit's `AppTest` harness (streamlit.testing.v1),
which executes each page for real against the fixture data and asserts no
exception. This catches runtime-only bugs (bad widget values, missing columns)
that a plain import check cannot.

The full-server boot test is marked `slow` and excluded from the default run
(see pyproject `-m "not slow"`); CI runs it in a dedicated job.
"""

import importlib
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

PAGES = [
    "app.Home",
    "app.pages.1_Dashboard",
    "app.pages.2_Transactions",
    "app.pages.3_Fraud_Detection",
    "app.pages.4_Ask_The_Agent",
    "app.pages.5_Reports",
    "app.pages.6_Settings",
    "app.pages.7_Trust_Transparency",
]

PAGE_FILES = [
    "app/Home.py",
    "app/pages/1_Dashboard.py",
    "app/pages/2_Transactions.py",
    "app/pages/3_Fraud_Detection.py",
    "app/pages/4_Ask_The_Agent.py",
    "app/pages/5_Reports.py",
    "app/pages/6_Settings.py",
    "app/pages/7_Trust_Transparency.py",
]


@pytest.fixture(scope="module", autouse=True)
def _ensure_fixture_data():
    """The AppTest render tests need real data + a model bundle on disk.

    On a fresh checkout the data file may not exist; generate it once here. The
    model bundle is required by the fraud page — if missing, train once (slow,
    warm-cache afterwards).
    """
    data_path = ROOT / "data" / "transactions.csv"
    if not data_path.exists():
        subprocess.run(
            [sys.executable, "generate_data.py", "--config", str(ROOT / "config.yaml")],
            cwd=str(ROOT),
            check=True,
        )
    bundle = ROOT / "model_bench" / "risk_model_bundle.joblib"
    if not bundle.exists():
        subprocess.run(
            [
                sys.executable,
                "model_bench/train_and_compare.py",
                "--data",
                str(data_path),
                "--config",
                str(ROOT / "config.yaml"),
            ],
            cwd=str(ROOT),
            check=True,
        )


@pytest.mark.parametrize("module_name", PAGES)
def test_pages_import_cleanly(module_name):
    module = importlib.import_module(module_name)
    assert hasattr(module, "render")


@pytest.mark.parametrize("page_file", PAGE_FILES)
def test_page_renders_headless_no_exception(page_file):
    """3.4: each page must actually render under AppTest without exceptions."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / page_file), default_timeout=120)
    at.run()
    assert not at.exception, f"{page_file} raised: {at.exception}"


def test_transactions_page_date_input_renders():
    """2.13: the date range must render with real date objects (no exception)."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app/pages/2_Transactions.py"), default_timeout=120)
    at.run()
    assert not at.exception
    assert at.dataframe


def test_fraud_page_renders_risk_scan_widgets():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app/pages/3_Fraud_Detection.py"), default_timeout=120)
    at.run()
    assert not at.exception
    # live-scan tab defaults to focal-only (2.12) — a toggle widget exists
    assert at.toggle


def test_agent_page_bootstraps_chat_state():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app/pages/4_Ask_The_Agent.py"), default_timeout=120)
    at.run()
    assert not at.exception
    assert at.chat_input


def test_agent_bootstraps_without_api_key():
    from finance_agent.agent import FinanceAgent

    agent = FinanceAgent(str(ROOT / "config.yaml"), api_key="")
    assert not agent.llm_available()
    assert agent.answer("hello").strip()


def test_settings_page_renders_key_and_data_tabs():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app/pages/6_Settings.py"), default_timeout=120)
    at.run()
    assert not at.exception
    assert at.text_input  # the API key field


def test_home_page_renders_with_auth_banner_when_no_password():
    """1.1: with APP_PASSWORD unset the app renders a DEMO MODE banner, not a lock."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app/Home.py"), default_timeout=120)
    at.run()
    assert not at.exception
    assert at.warning, "expected the DEMO MODE banner when APP_PASSWORD is unset"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.slow
def test_streamlit_app_boots_headless():
    """Start `streamlit run app/Home.py` and poll the health endpoint."""
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "app/Home.py",
            "--server.headless",
            "true",
            "--server.port",
            str(port),
            "--browser.gatherUsageStats",
            "false",
        ],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 90
        healthy = False
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"Streamlit exited early:\n{out[-2000:]}")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/_stcore/health", timeout=2
                ) as resp:
                    if resp.read().decode() == "ok":
                        healthy = True
                        break
            except Exception:  # noqa: BLE001 — server still starting
                time.sleep(2)
        assert healthy, "Streamlit did not become healthy in time"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
