"""Dashboard — KPI cards, spend-by-category donut, income-vs-expense trend,
and an auto-generated 'top 3 things to know this month' callout."""

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from app import common

CATEGORY_COLORS = [
    "#34D399",
    "#4C9EEB",
    "#F59E0B",
    "#A78BFA",
    "#F472B6",
    "#60A5FA",
    "#F87171",
    "#FBBF24",
    "#94A3B8",
]


def _kpis(months: pd.DataFrame, month: str) -> None:
    row = months[months["month"] == month].iloc[0]
    prev = months[months["month"] < month].tail(1)
    prev = prev.iloc[0] if not prev.empty else None
    facts = common.get_facts()
    health = facts.financial_health()["data"]

    def delta(cur: float, past: float | None, invert: bool = False) -> tuple[str, str]:
        if past is None:
            return "", "neutral"
        diff = cur - past
        tone = "pos" if (diff >= 0) != invert else "neg"
        sign = "+" if diff >= 0 else ""
        return f"{sign}${diff:,.0f} vs last month", tone

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        d, t = delta(row["income"], prev["income"] if prev is not None else None)
        common.kpi_card("Income", f"${row['income']:,.0f}", d, t)
    with c2:
        d, t = delta(row["expenses"], prev["expenses"] if prev is not None else None, invert=True)
        common.kpi_card("Expenses", f"${row['expenses']:,.0f}", d, t)
    with c3:
        d, t = delta(row["net"], prev["net"] if prev is not None else None)
        common.kpi_card("Net", f"${row['net']:,.0f}", d, t)
    with c4:
        rate = row["net"] / row["income"] if row["income"] else 0
        common.kpi_card(
            "Savings rate", f"{rate * 100:.1f}%", f"health score {health['score']}/100", "neutral"
        )
    with c5:
        common.kpi_card(
            "Health score",
            f"{health['score']}/100",
            f"buffer ~{health['buffer_months']:.1f} months",
            "neutral",
        )


def _donut(month: str, account_type: str | None = None) -> None:
    facts = common.get_facts()
    cat = facts.category_breakdown(month, account_type=account_type)["data"]
    if not cat["rows"]:
        st.info("No spending recorded for this month.")
        return
    rows = pd.DataFrame(cat["rows"])
    fig = px.pie(
        rows,
        names="category",
        values="amount",
        hole=0.58,
        color_discrete_sequence=CATEGORY_COLORS,
        title=f"Spending by category — {month}",
    )
    fig.update_traces(textinfo="label+percent", textfont_size=11)
    fig.update_layout(
        showlegend=False,
        margin=dict(t=40, b=10, l=10, r=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font_color="#E7EEF9" if common.theme() == "dark" else "#0F172A",
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def _budget_tracker(month: str, account_type: str | None = None) -> None:
    """Per-category budget goals with color-coded progress bars."""
    facts = common.get_facts()
    status = facts.budget_status(month, account_type=account_type)["data"]
    if not status.get("configured"):
        return
    rows = status["rows"]
    if not rows:
        return

    st.markdown("### 🎯 Budget tracker")
    cols = st.columns(2, gap="large")
    for i, r in enumerate(rows):
        col = cols[i % 2]
        with col:
            pct = min(r["pct"], 1.0)
            if r["over"]:
                bar_color = "#EF4444"
                flag = " <span class='risk-flag'>over goal</span>"
            elif r["pct"] >= 0.8:
                bar_color = "#F59E0B"
                flag = ""
            else:
                bar_color = "#34D399"
                flag = ""
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;font-size:0.85rem'>"
                f"<span style='font-weight:600'>{common.esc(r['category'])}</span>"
                f"<span style='color:#8A97B0'>${r['spent']:,.0f} / ${r['goal']:,.0f}"
                f" · {r['pct'] * 100:.0f}%</span>{flag}</div>"
                f"<div style='background:#1E2A44;border-radius:4px;height:8px;margin:4px 0 12px'>"
                f"<div style='background:{bar_color};border-radius:4px;height:8px;"
                f"width:{pct * 100:.1f}%'></div></div>",
                unsafe_allow_html=True,
            )


def _trend(months: pd.DataFrame) -> None:
    fig = go.Figure()
    fig.add_bar(
        x=months["month"],
        y=months["expenses"],
        name="Expenses",
        marker_color="#F59E0B",
        marker_line_width=0,
    )
    fig.add_bar(
        x=months["month"],
        y=months["income"],
        name="Income",
        marker_color="#34D399",
        marker_line_width=0,
    )
    fig.add_scatter(
        x=months["month"],
        y=months["net"],
        name="Net",
        mode="lines+markers",
        line=dict(color="#4C9EEB", width=3),
    )
    fig.update_layout(
        title="Income vs expenses across months",
        barmode="group",
        margin=dict(t=40, b=10, l=10, r=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=1.12),
        font_color="#E7EEF9" if common.theme() == "dark" else "#0F172A",
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def render() -> None:
    st.set_page_config(
        page_title="Dashboard · FinSight Agent",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    common.inject_css()
    common.require_auth()
    common.ensure_data()

    facts = common.get_facts()
    # Account-type filter (multi-account data): the default view is the primary
    # checking account; "All accounts" spans checking + savings + credit.
    account_types = facts.account_types()
    if len(account_types) > 1:
        labels = ["All accounts"] + [t.capitalize() for t in account_types]
        choice = st.sidebar.radio("Account", labels, index=0, horizontal=True)
        # The radio stub allows None even though a non-empty label list always
        # yields a string; `choice or ""` keeps the account filter total on None.
        acct: str | None = "all" if choice == "All accounts" else (choice or "").lower()
    else:
        acct = None

    months = common.monthly_table(acct or "checking")
    available = months["month"].tolist()
    month = st.sidebar.selectbox("Month", available, index=len(available) - 1) if available else ""

    common.page_header("Dashboard", f"Your finances for {month}")
    if not available:
        st.info("No transactions yet — regenerate data in Settings.")
        return

    _kpis(months, month)

    left, right = st.columns([3, 2], gap="large")
    with left:
        _trend(months)
    with right:
        _donut(month, acct)

    _budget_tracker(month, acct)

    facts = common.get_facts()
    tips = facts.top_tips()["data"]["tips"]
    common.callout(
        "Top 3 things to know",
        "<br>".join(f"{i + 1}. {common.esc(t)}" for i, t in enumerate(tips)),
    )

    if facts.rule_only():
        st.warning(
            "⚠️ **Rule-only mode — model unavailable.** No trained model bundle is loaded, "
            "so risk scoring uses rules alone (risk = rule score). Run `make train` or open "
            "Fraud & Anomaly Detection to train one."
        )
    agent = common.get_agent(common.api_key())
    if not agent.llm_available():
        st.caption(
            "ℹ️ Offline narrator (no valid ANTHROPIC_API_KEY) — set one in Settings for the LLM agent."
        )


if __name__ == "__main__":
    common.run_render(render)
