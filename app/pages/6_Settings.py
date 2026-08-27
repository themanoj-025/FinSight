"""Settings — API key (session-only), theme toggle, agent model, and data regeneration.

Config writes are gated behind `require_auth()` and validated against an
allowlist — arbitrary free-text never reaches `yaml.safe_dump`.
"""

import streamlit as st

from app import common
from finance_agent.constants import DEFAULT_MODEL, MODEL_ALLOWLIST


def _regenerate(seed: int, days: int, n_bg: int) -> None:
    common.run_python(
        [
            "generate_data.py",
            "--seed",
            str(seed),
            "--days",
            str(days),
            "--n-background-accounts",
            str(n_bg),
            "--config",
            str(common.ROOT / "config.yaml"),
        ]
    )
    common.clear_all_caches()
    st.session_state.messages = []
    st.rerun()


def _toggle_theme(t) -> None:
    st.session_state.theme = t
    st.rerun()


def _usage_tab() -> None:
    """Per-session LLM observability: real tokens, latency, est. cost."""
    agent = common.get_agent(common.api_key())
    totals = agent.usage_summary()
    st.markdown(
        "Real per-session LLM accounting — token counts come from the API's usage "
        "payload (not the char-based budget estimate), latency is wall-clock, and "
        "cost is estimated from `config.yaml agent.pricing`. Offline-narrator turns "
        "are tracked at $0 so the ledger is honest about what the session spent."
    )

    if totals["calls"] == 0:
        st.info(
            "No agent activity yet this session — ask a question on the "
            "**Ask the Agent** page and the numbers will appear here."
        )
        return

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric(
            "LLM calls",
            f"{totals['llm_calls']}",
            f"{totals['narrator_calls']} narrator (free)",
        )
    with c2:
        st.metric("Est. cost", f"${totals['est_cost']:.4f}")
    with c3:
        st.metric(
            "Tokens (real)",
            f"{totals['input_tokens'] + totals['output_tokens']:,}",
            f"{totals['input_tokens']:,} in · {totals['output_tokens']:,} out",
        )
    with c4:
        st.metric(
            "Avg latency",
            f"{totals['avg_latency_ms']:.0f} ms",
            f"{totals['tool_calls']} tool calls · {totals['failed_calls']} failed",
        )

    if totals["input_tokens"] or totals["output_tokens"]:
        st.caption(
            "Budget-side estimate (approx tokens) would be "
            f"{totals['input_tokens'] + totals['output_tokens']} — the API's real "
            "counts are shown above."
        )

    calls = agent.usage_recent(30)
    if calls:
        rows = [
            {
                "#": i,
                "Kind": c["kind"],
                "Model": c["model"],
                "In": c["input_tokens"],
                "Out": c["output_tokens"],
                "Latency ms": c["latency_ms"],
                "Est. cost": f"${c['est_cost']:.6f}",
            }
            for i, c in enumerate(reversed(calls), start=1)
        ]
        st.dataframe(rows, hide_index=True, use_container_width=True)
        st.caption("Most recent 30 calls (newest first).")

    budget = st.session_state.get("budget")
    if budget is not None:
        st.progress(
            budget.turns_used / budget.max_turns if budget.max_turns else 0.0,
            text=f"Session budget: {budget.turns_used}/{budget.max_turns} turns · "
            f"{budget.tokens_used:,}/{budget.max_tokens:,} approx tokens",
        )

    if st.button("🧹 Reset session usage"):
        agent.usage_reset()
        st.session_state.pop("budget", None)
        st.rerun()


def _key_tab() -> None:
    st.markdown(
        "The API key is held **only in this browser session** — it is never written to "
        "disk. Without a key the agent runs in **offline narrator mode**, which answers "
        "the same questions with deterministic templates."
    )
    key = st.text_input(
        "Anthropic API key",
        type="password",
        value=common.api_key(),
        placeholder="sk-ant-…",
        help="Get one at console.anthropic.com",
    )
    if key != common.api_key():
        st.session_state.api_key = key
        common.clear_agent_cache()
        st.rerun()

    agent = common.get_agent(key)
    if not key:
        st.info("No key set — running the deterministic offline narrator.")
    else:
        # Real validation (1.5): don't claim "connected" until the key passes an
        # auth check. The result is cached per key for the session.
        with st.spinner("Validating key…"):
            valid = agent.validate_api_key(key)
        if valid:
            st.success("LLM agent connected ✓ — answers will be grounded by tool use.")
        else:
            st.error("Invalid key — check it and try again (the offline narrator will be used).")

    st.markdown("### Agent model")
    model = st.text_input("Model ID", value=model_or_default())
    if st.button("Save model to config.yaml", type="primary"):
        if model in MODEL_ALLOWLIST:
            _set_model(model)
            st.success(f"Saved {model} to config.yaml — restart the app to pick it up.")
        else:
            st.error(
                f"Unknown model id {model!r}. Allowed: "
                + ", ".join(MODEL_ALLOWLIST)
                + ". The config file was left untouched."
            )


def render() -> None:
    st.set_page_config(
        page_title="Settings · FinSight Agent",
        page_icon="⚙️",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    common.inject_css()
    common.require_auth()
    common.ensure_data()

    common.page_header("Settings", "Session-only preferences and data regeneration")

    tab_keys, tab_usage, tab_app, tab_data = st.tabs(
        ["🔑 API key", "📊 Session usage", "🎨 Appearance", "🔄 Data"]
    )

    with tab_keys:
        _key_tab()

    with tab_usage:
        _usage_tab()

    with tab_app:
        current = common.theme()
        choice = st.radio(
            "Theme", ["dark", "light"], index=0 if current == "dark" else 1, horizontal=True
        )
        if choice != current:
            _toggle_theme(choice)
        st.markdown(
            "Palette: deep ink navy with a single emerald accent for growth figures — the "
            "amber/red warning colors are **reserved for fraud & risk flags only**, so they "
            "keep their meaning."
        )

    with tab_data:
        facts = common.get_facts()
        st.markdown(
            f"**{len(facts.df):,} transactions** · {int(facts.df['isFraud'].sum())} fraud "
            f"({facts.df['isFraud'].mean() * 100:.1f}%) · "
            f"{int(facts.df['is_anomaly'].sum())} injected anomalies · seed "
            f"{facts.cfg.get('data', {}).get('seed')}."
        )
        c1, c2, c3 = st.columns(3)
        with c1:
            seed = st.number_input("Seed", 0, 1_000_000, 42)
        with c2:
            days = st.number_input("Days", 30, 730, 90)
        with c3:
            n_bg = st.number_input("Background accounts", 10, 1000, 150)
        if st.button("🔄 Regenerate synthetic data", type="primary"):
            with st.spinner("Generating a new ledger…"):
                _regenerate(int(seed), int(days), int(n_bg))
        st.caption(
            "After regeneration the model bundle is stale — retrain it in "
            "Fraud & Anomaly Detection or via `make train`."
        )


def model_or_default() -> str:
    try:
        return str(common.get_facts().cfg.get("agent", {}).get("model", DEFAULT_MODEL))
    except (AttributeError, ValueError, KeyError):
        return DEFAULT_MODEL


def _set_model(model: str) -> None:
    import yaml

    path = common.ROOT / "config.yaml"
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("agent", {})["model"] = model
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)
    common.clear_facts_cache()


if __name__ == "__main__":
    common.run_render(render)
