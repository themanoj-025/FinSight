"""Ask the Agent — a chat interface with streaming responses, suggested
questions, conversation history (passed to the agent for real multi-turn
context), a per-session LLM budget, and a live agent activity log.

The LLM budget is persisted server-side in SQLite keyed by a session id that
lives in the URL query string, so reloading the page cannot silently reset it
(C.1.2) — enforcement uses the exact token counts the API reports.
"""

import os
import uuid

import streamlit as st

from app import common

SUGGESTIONS = [
    "Any suspicious activity?",
    "How can I save more?",
    "Forecast next month",
    "What are my recurring payments?",
]


def _session_id() -> str:
    """A stable per-browser session id carried in the URL query string.

    st.session_state resets on a full page reload; the query string survives
    it, so the persisted budget keeps counting rather than restarting.
    """
    qp = st.query_params
    sid = qp.get("sid")
    if not sid:
        qp["sid"] = uuid.uuid4().hex
        sid = qp.get("sid")
    return str(sid)


def _get_budget(agent):
    """The persisted per-session budget (cached in session state)."""
    budget = st.session_state.get("budget")
    if budget is None:
        budget = agent.session_budget(_session_id())
        st.session_state.budget = budget
    return budget


def _suggested_chips(agent) -> None:
    st.caption("Try asking:")
    cols = st.columns(len(SUGGESTIONS))
    for col, q in zip(cols, SUGGESTIONS, strict=True):
        with col:
            if st.button(q, key=f"chip_{q}", width="stretch"):
                st.session_state.pending = q


def _stream_answer(agent, question: str, history: list[dict], budget) -> str:
    """Render the agent's answer (streamed when possible) and return the full text."""
    stream = agent.answer(question, history=history, stream=True, budget=budget)
    if hasattr(st, "write_stream"):
        out = st.write_stream(stream)
        return "".join(out) if isinstance(out, list) else out
    holder = st.empty()  # pragma: no cover — older streamlit
    acc = ""
    for chunk in stream:
        acc += chunk
        holder.markdown(acc)
    return acc


def _budget_sidebar(agent) -> None:
    """Per-session LLM budget (config agent.max_session_turns/max_session_tokens).

    Persisted in SQLite keyed by session id — the token figures are the exact
    counts the Anthropic API reported, not a chars/4 estimate, and they
    survive page reloads (C.1.2).
    """
    with st.sidebar.expander("💸 LLM session budget"):
        if not agent.llm_available():
            st.write("LLM not active — offline narrator has no budget.")
            return
        budget = _get_budget(agent)
        st.progress(
            budget.turns_used() / budget.max_turns if budget.max_turns else 0.0,
            text=f"Turns: {budget.turns_used()}/{budget.max_turns}",
        )
        st.progress(
            budget.tokens_used() / budget.max_tokens if budget.max_tokens else 0.0,
            text=f"Tokens (exact, persisted): {budget.tokens_used():,}/{budget.max_tokens:,}",
        )
        totals = agent.usage_summary()
        if totals["calls"]:
            st.caption(
                f"Est. cost: ${totals['est_cost']:.4f} · avg "
                f"{totals['avg_latency_ms']:.0f} ms/call · "
                f"{totals['input_tokens'] + totals['output_tokens']:,} real tokens"
            )
        if reason := budget.exhausted_reason():
            st.warning(f"Budget exhausted ({reason}) — falling back to the offline narrator.")


def render() -> None:
    st.set_page_config(
        page_title="Ask the Agent · FinSight Agent",
        page_icon="💬",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    common.inject_css()
    common.require_auth()
    common.ensure_data()

    agent = common.get_agent(common.api_key())
    mode = (
        "LLM agent"
        if agent.llm_available()
        else "offline narrator — no valid ANTHROPIC_API_KEY set"
    )
    common.page_header(
        "Ask the Agent", f"Plain-English questions, answers grounded in real tool outputs · {mode}"
    )

    # Public-instance banner (E.1 §3a): a deployed FinSight never carries a
    # server-side Anthropic key, so it must say so plainly and point users to
    # BYO-key mode instead of failing confusingly. Local runs (FINSIGHT_PUBLIC
    # unset) keep the existing quiet caption.
    if not agent.llm_available() and os.environ.get("FINSIGHT_PUBLIC") == "1":
        st.info(
            "🔑 **Public demo — bring your own key.** This instance runs without a "
            "server-side Anthropic key. Paste one in **Settings** to enable the LLM "
            "agent, or explore in **offline narrator mode** (deterministic, no "
            "network, no API cost)."
        )

    _budget_sidebar(agent)

    if "messages" not in st.session_state:
        st.session_state.messages = []
    pending = st.session_state.pop("pending", None)

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    question = pending or st.chat_input("Ask about your finances…")
    if question:
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            # Pass the real conversation so turn 2 can reference turn 1's context.
            history = [
                {"role": m["role"], "content": m["content"]} for m in st.session_state.messages[:-1]
            ]
            budget = _get_budget(agent)
            reply = _stream_answer(agent, question, history, budget)
        st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()

    _suggested_chips(agent)

    with st.sidebar.expander("🤖 Agent activity log"):
        st.caption("Every tool call the agent made (proof it uses real data, not vibes):")
        log = agent.activity_log(20)
        if not log:
            st.write("No tool calls yet — ask a question first.")
        for entry in reversed(log):
            args = ", ".join(f"{k}={v}" for k, v in (entry.get("args") or {}).items())
            st.markdown(
                f"**{entry['tool']}** · {entry['latency_ms']:.0f} ms"
                f"{(' · ' + args) if args else ''}"
                f"{' · ⚠️ ' + entry['error'] if not entry['ok'] else ''}"
            )


if __name__ == "__main__":
    common.run_render(render)
