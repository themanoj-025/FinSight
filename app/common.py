"""Shared helpers for the Streamlit app: cached loading, styling, KPI cards.

Everything expensive (data, model bundle, facts, agent) is cached so the app
feels instant. If artifacts are missing, the app bootstraps them with a visible
spinner rather than crashing.

Auth: `require_auth()` implements a demo-grade password gate (APP_PASSWORD env
var). It is not enterprise auth — it exists so a public deployment isn't wide
open by default, and the gap is documented in docs/KNOWN_LIMITATIONS.md.
"""

from __future__ import annotations

import hmac
import html
import logging
import os
import subprocess
import sys
import time as _time
import uuid
from pathlib import Path

import pandas as pd
import streamlit as st

from finance_agent.observability import configure_logging, report_exception, set_correlation_id

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # allow `from app import common` regardless of cwd

ACCENT = "#34D399"
WARNING = "#F59E0B"
DANGER = "#EF4444"
MUTED = "#8A97B0"


# ------------------------------------------------------- observability (D.1)
log = logging.getLogger("app")


def ensure_correlation_id() -> str:
    """The session-scoped correlation id, minted once per Streamlit session.

    Every structured log line emitted while rendering pages in this session
    carries the id, so one id reconstructs the whole user session (D.1).
    """
    configure_logging()
    cid = st.session_state.get("correlation_id") or uuid.uuid4().hex[:12]
    st.session_state["correlation_id"] = cid
    set_correlation_id(cid)
    return cid


def run_render(render_fn) -> None:
    """Run a page's ``render()`` inside the observability boundary.

    Binds the session correlation id, then executes the page. Any uncaught
    exception is logged as structured JSON with the id and reported to Sentry
    when ``SENTRY_DSN`` is set (inert otherwise — D.1), then re-raised so
    Streamlit's own error UI still shows and AppTest still sees the failure.
    """
    ensure_correlation_id()
    try:
        render_fn()
    except Exception as exc:  # noqa: BLE001 — page boundary: log + report, then re-raise
        log.error("page render failed: %s", exc, exc_info=True)
        report_exception(exc)
        raise


# ---------------------------------------------------------------- bootstrap
def _run_python(args: list[str]) -> None:
    subprocess.run(
        [sys.executable, *args], cwd=str(ROOT), check=True, capture_output=True, text=True
    )


def api_url() -> str:
    """Base URL of the FinSight Agent API, or '' to use local facts (default)."""
    return os.environ.get("FINSIGHT_API_URL", "").strip()


def finsight_api_key() -> str:
    """Optional shared secret for the FinSight API (X-API-Key header)."""
    return os.environ.get("FINSIGHT_API_KEY", "").strip()


def _selected_focal_user() -> str:
    """The focal user selected in the sidebar ('' = config default)."""
    return st.session_state.get("focal_user", "")


@st.cache_resource(show_spinner=False)
def _facts_for(focal_user: str):
    """Cached per-user facts: the FinSight API when FINSIGHT_API_URL is set, else local.

    In service mode the app is a *client* of finance_agent/api.py (docker compose
    wires this up); if the API is unreachable we degrade to local facts with a
    visible warning rather than crashing.
    """
    url = api_url()
    if url:
        from app.api_client import ApiClient, ApiClientError

        client = ApiClient(url, api_key=finsight_api_key(), timeout=5.0, focal_user=focal_user)
        try:
            client.ping()
            return client
        except ApiClientError:
            st.warning(
                f"⚠️ FinSight API at {url} is unreachable — falling back to local "
                "facts (offline mode). Check that the API service is running."
            )
    from finance_agent.tools import FinanceFacts

    return FinanceFacts(str(ROOT / "config.yaml"), focal_user=focal_user or None)


def get_facts():
    """Per-user cached facts (sidebar switcher changes the cache key)."""
    return _facts_for(_selected_focal_user())


def data_source() -> str:
    """'api' when facts are served by the FinSight Agent API, else 'local'."""
    from app.api_client import ApiClient

    return "api" if isinstance(get_facts(), ApiClient) else "local"


def focal_user_selector() -> None:
    """Sidebar selectbox to switch the focal user (multi-user mode).

    Hidden when the data has only one focal user; on change it reruns with the
    new selection, which re-keys the facts cache.
    """
    facts = get_facts()
    from app.api_client import ApiClient

    if isinstance(facts, ApiClient):
        current, users = facts._resolve_focal()  # noqa: SLF001 — client helper
    else:
        users = list(getattr(facts, "focal_users", []) or [])
        current = _selected_focal_user() or str(
            getattr(facts, "focal_user", users[0] if users else "")
        )
    if len(users) < 2:
        return
    if current not in users:
        current = users[0]
    with st.sidebar:
        choice = st.selectbox("Focal user", users, index=users.index(current))
    if choice != current:
        st.session_state["focal_user"] = choice
        st.rerun()


@st.cache_resource(show_spinner=False)
def _agent_for(api_key: str, focal_user: str):
    from finance_agent.agent import FinanceAgent

    return FinanceAgent(str(ROOT / "config.yaml"), api_key=api_key, focal_user=focal_user or None)


def get_agent(api_key: str = ""):
    return _agent_for(api_key, _selected_focal_user())


@st.cache_data(show_spinner=False)
def _monthly_table(focal_user: str, account_type: str = "checking") -> pd.DataFrame:
    # C.2.5: the dashboard table and the facts tools must compute identical
    # income/expenses/net figures — delegate to the shared module helper in
    # finance_agent/tools.py instead of reimplementing the column math here.
    from finance_agent.tools import monthly_income_expenses

    facts = _facts_for(focal_user)
    d = facts._focal(account_type=account_type or None)  # noqa: SLF001 — app helper
    return monthly_income_expenses(d)


def monthly_table(account_type: str = "checking") -> pd.DataFrame:
    return _monthly_table(_selected_focal_user(), account_type)


def clear_all_caches() -> None:
    """Clear every cache the app and tools hold, after data/model regeneration."""
    st.cache_data.clear()
    _facts_for.clear()
    _agent_for.clear()
    _monthly_table.clear()
    try:
        from finance_agent import tools as _tools

        _tools._scored_frame_json.cache_clear()  # noqa: SLF001
    except (OSError, ValueError):
        pass
    # The SQLite store self-heals via its (data, model) fingerprint, but drop
    # its rows immediately so a regenerated ledger can never serve stale scores
    # even if a fingerprint collides (e.g. file copied with a preserved mtime).
    try:
        from finance_agent.storage import reset_store_for_config

        reset_store_for_config(str(ROOT / "config.yaml"))
    except (OSError, ValueError):
        pass
    # In service mode, tell the API to drop its facts snapshot too so the next
    # request rebuilds it from the regenerated artifacts. Best-effort.
    if api_url():
        try:
            from app.api_client import ApiClient, ApiClientError

            ApiClient(api_url(), api_key=finsight_api_key(), timeout=5.0).reload()
        except ApiClientError:
            pass  # the API will pick up the new artifacts on its next restart


def ensure_data() -> None:
    data_path = ROOT / "data" / "transactions.csv"
    if not data_path.exists():
        with st.spinner("Generating synthetic transaction data…"):
            _run_python(["generate_data.py", "--config", str(ROOT / "config.yaml")])
        clear_all_caches()


def ensure_model() -> None:
    bundle = ROOT / "model_bench" / "risk_model_bundle.joblib"
    if not bundle.exists():
        with st.spinner("Training + benchmarking models (first run, ~30s)…"):
            _run_python(
                [
                    "model_bench/train_and_compare.py",
                    "--data",
                    str(ROOT / "data" / "transactions.csv"),
                    "--config",
                    str(ROOT / "config.yaml"),
                ]
            )
        clear_all_caches()


def api_key() -> str:
    return st.session_state.get("api_key", "")


def theme() -> str:
    return st.session_state.get("theme", "dark")


# ------------------------------------------------------------------------ auth
# Brute-force guard (audit §1/§5): per-session failed-attempt tracking with a
# cooldown. This is per-browser-session (Streamlit session_state), so it stops
# a scripted guesser hammering one browser session — real IP-level throttling
# needs a reverse proxy (documented in docs/KNOWN_LIMITATIONS.md).
_AUTH_MAX_ATTEMPTS = 5
_AUTH_WINDOW_SECONDS = 15 * 60  # failures counted within this rolling window
_AUTH_COOLDOWN_SECONDS = 5 * 60  # lockout length after the window fills


def esc(value: object) -> str:
    """HTML-escape a value for safe interpolation into ``unsafe_allow_html`` markup.

    The Streamlit UI renders a few data-driven strings (category names, tips,
    feature names) inside hand-written HTML. The ledger is synthetic today,
    but these values are data, not code — escaping at the boundary is what
    keeps a future data source (or a compromised CSV) from turning into
    stored XSS (audit §4).
    """
    return html.escape(str(value), quote=True)


def require_auth() -> None:
    """Demo-grade password gate (1.1): APP_PASSWORD env var or a clear banner.

    This is deliberately minimal — a shared password in an env var, checked in
    session state, with a per-session brute-force cooldown. It is NOT a
    substitute for real auth; see docs/KNOWN_LIMITATIONS.md for what
    production would need.
    """
    expected = os.environ.get("APP_PASSWORD", "")
    if not expected:
        if not st.session_state.get("_demo_banner_shown"):
            st.session_state["_demo_banner_shown"] = True
            st.warning(
                "⚠️ DEMO MODE — NOT SECURED. `APP_PASSWORD` is not set, so this "
                "instance is open to anyone who can reach it. Set the env var for a "
                "password gate (demo-grade only)."
            )
        return
    if st.session_state.get("authenticated"):
        return
    now = _time.time()
    attempts = [
        t for t in st.session_state.get("_auth_attempts", []) if now - t < _AUTH_WINDOW_SECONDS
    ]
    st.session_state["_auth_attempts"] = attempts
    if len(attempts) >= _AUTH_MAX_ATTEMPTS:
        wait = int(_AUTH_COOLDOWN_SECONDS - (now - attempts[0]))
        if wait > 0:
            st.error(f"Too many failed attempts — locked out for ~{wait} s. Try again later.")
            log.warning("app auth lockout engaged")
            st.stop()
        st.session_state["_auth_attempts"] = []  # cooldown elapsed: retry window resets
    st.markdown('<div class="hero-title">FinSight Agent</div>', unsafe_allow_html=True)
    st.markdown("Enter the shared password to continue.")
    pw = st.text_input("Password", type="password")
    if st.button("Unlock", type="primary"):
        # Constant-time comparison (C.1.1): a plain == on a shared secret is
        # vulnerable to a timing side channel.
        if hmac.compare_digest(pw or "", expected):
            st.session_state["authenticated"] = True
            st.session_state.pop("_auth_attempts", None)
            log.info("app auth success")
            st.rerun()
        elif pw:
            # Audit §8: auth failures are logged server-side (no password ever
            # touches the log) and counted toward the per-session cooldown.
            st.session_state.setdefault("_auth_attempts", []).append(now)
            log.warning("app auth failure")
            st.error("Incorrect password.")
    st.stop()


# ------------------------------------------------------------------- styling
def inject_css() -> None:
    is_light = theme() == "light"
    bg = "#F4F7FB" if is_light else "#0A0F1E"
    panel = "#FFFFFF" if is_light else "#111A2E"
    border = "#DCE4F0" if is_light else "#1E2A44"
    text = "#0F172A" if is_light else "#E7EEF9"
    muted = "#64748B" if is_light else "#8A97B0"
    # st.code token color (F.4): Streamlit's Prism theme paints `.token.function`
    # etc. #1c83e1 on #1a1c24 — 4.35:1, just under the WCAG AA 4.5:1 floor, so
    # axe flags it. Theme-aware override with sufficient contrast on both
    # themes (dark #58A6FF ≈ 6.7:1 on #1a1c24; light #1F5FA8 ≈ 5.8:1 on white).
    code_token = "#1F5FA8" if is_light else "#58A6FF"
    st.markdown(
        f"""
<style>
  html, body, [class*="css"] {{
    font-family: "Segoe UI", "Inter", system-ui, -apple-system, sans-serif;
  }}
  .stApp {{ background: {bg}; color: {text}; }}
  h1, h2, h3 {{ letter-spacing: -0.02em; color: {text}; }}

  .kpi-card {{
    background: linear-gradient(150deg, {panel}, {bg});
    border: 1px solid {border};
    border-radius: 16px;
    padding: 18px 20px 14px;
    margin-bottom: 10px;
    box-shadow: 0 4px 18px rgba(0,0,0,0.12);
    transition: transform 0.15s ease;
  }}
  .kpi-card:hover {{ transform: translateY(-2px); }}
  .kpi-label {{ font-size: 0.78rem; letter-spacing: 0.08em; text-transform: uppercase;
               color: {muted}; }}
  .kpi-value {{ font-size: 1.65rem; font-weight: 700; margin-top: 2px; color: {text}; }}
  .kpi-delta {{ font-size: 0.8rem; margin-top: 2px; }}
  .kpi-delta.pos {{ color: {ACCENT}; }}
  .kpi-delta.neg {{ color: {DANGER}; }}
  .kpi-delta.neutral {{ color: {muted}; }}

  .callout {{
    border-left: 4px solid {ACCENT};
    background: {panel};
    border-radius: 12px;
    padding: 14px 18px;
    margin: 8px 0 18px;
    color: {text};
  }}
  .callout .callout-title {{ font-weight: 700; margin-bottom: 6px; }}

  .hero-title {{ font-size: 2.6rem; font-weight: 800; letter-spacing: -0.03em; color: {text}; }}
  .hero-sub {{ color: {muted}; font-size: 1.05rem; margin-top: -6px; }}
  .pill {{ display: inline-block; background: {panel}; border: 1px solid {border};
           border-radius: 999px; padding: 3px 12px; font-size: 0.75rem; color: {muted};
           margin-right: 6px; }}

  .layer {{ background: {panel}; border: 1px solid {border}; border-radius: 14px;
           padding: 14px 18px; margin: 8px 0; }}
  .layer .layer-name {{ font-weight: 700; color: {ACCENT}; font-size: 0.85rem;
                       letter-spacing: 0.06em; text-transform: uppercase; }}
  .layer p {{ margin: 4px 0 0; color: {muted}; font-size: 0.9rem; }}

  .risk-flag {{ color: {DANGER}; font-weight: 700; }}
  .ok-text {{ color: {ACCENT}; }}

  [data-testid="stSidebar"] {{ background: {panel}; }}
  [data-testid="stMetricValue"] {{ color: {text}; }}
  .stChatMessage {{ background: {panel}; border: 1px solid {border}; }}

  /* st.code blocks (F.4): Streamlit's Prism token palette paints `.token.function`
     etc. #1c83e1 on #1a1c24 (4.35:1, under the WCAG AA 4.5 floor) and the <code>
     carries an inline `white-space: pre` that makes the <pre> scroll horizontally
     (axe: color-contrast + scrollable-region-focusable). The block's testid is
     `stCode`; `!important` beats the inline style. Theme-aware high-contrast
     token color + wrapping so the block is never a scrollable region. */
  [data-testid="stCode"] code {{ white-space: pre-wrap !important; overflow-wrap: anywhere !important; }}
  [data-testid="stCode"] pre {{ overflow-x: hidden; }}
  [data-testid="stCode"] .token.function,
  [data-testid="stCode"] .token.builtin,
  [data-testid="stCode"] .token.class-name {{ color: {code_token} !important; }}
</style>
""",
        unsafe_allow_html=True,
    )


def kpi_card(label: str, value: str, delta: str = "", tone: str = "neutral") -> None:
    cls = {"pos": "pos", "neg": "neg", "neutral": "neutral"}.get(tone, "neutral")
    st.markdown(
        f'<div class="kpi-card">'
        f'<div class="kpi-label">{esc(label)}</div>'
        f'<div class="kpi-value">{esc(value)}</div>'
        f'<div class="kpi-delta {cls}">{esc(delta)}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )


def callout(title: str, body: str) -> None:
    # `title` is escaped; `body` is a raw-HTML slot by contract — callers must
    # escape any data they embed in it (e.g. via `esc()`).
    st.markdown(
        f'<div class="callout"><div class="callout-title">{esc(title)}</div>{body}</div>',
        unsafe_allow_html=True,
    )


def page_header(title: str, subtitle: str = "") -> None:
    focal_user_selector()
    st.markdown(
        f'<div class="hero-title" style="font-size:1.9rem">{esc(title)}</div>',
        unsafe_allow_html=True,
    )
    if subtitle:
        st.markdown(
            f'<div class="hero-sub" style="margin-top:2px">{esc(subtitle)}</div>',
            unsafe_allow_html=True,
        )
    if data_source() == "api":
        st.caption(f"ℹ️ Data served by the FinSight Agent API ({api_url()})")
    st.write("")
