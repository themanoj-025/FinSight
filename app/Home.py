"""FinSight Agent — landing page (app entry point)."""

import streamlit as st

from app import common


def render() -> None:
    st.set_page_config(
        page_title="FinSight Agent",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    common.inject_css()
    common.require_auth()
    common.ensure_data()
    common.focal_user_selector()

    st.markdown('<div class="hero-title">FinSight Agent</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="hero-sub">Turns raw bank transactions into fraud alerts, spending '
        "insight, and plain-English advice — autonomously.</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        '<span class="pill">hybrid rules + ML + LLM</span>'
        '<span class="pill">model benchmark, PR-AUC-first</span>'
        '<span class="pill">fully offline fallback</span>'
        '<span class="pill">synthetic data · no downloads</span>',
        unsafe_allow_html=True,
    )
    st.write("")

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("What it does")
        st.write(
            "FinSight Agent is an end-to-end, agentic personal-finance system. It generates a "
            "realistic PaySim-style transaction ledger, benchmarks six fraud-detection models, "
            "auto-selects the best one by PR-AUC, and wraps everything in a hybrid agent you can "
            "question in plain English — with every number it says traceable to a tool output."
        )
        st.subheader("How it's built")
        st.markdown(
            '<div class="layer"><div class="layer-name">Facts layer — deterministic Python</div>'
            "<p>Rule detectors (balance drains, duplicate charges, spend spikes, recurring "
            "payments), feature engineering, the trained model, and a blended risk score "
            "per transaction.</p></div>"
            '<div class="layer"><div class="layer-name">Reasoning layer — Claude tool use</div>'
            "<p>The LLM decides which tools to call for a question, we execute them, and it only "
            "writes narrative from their outputs. Without an API key it degrades to an offline "
            "narrator — same answers, no network.</p></div>"
            '<div class="layer"><div class="layer-name">Presentation — Streamlit</div>'
            "<p>Dashboard, transactions explorer, model-comparison + live risk scan, chat, "
            "reports, and settings.</p></div>",
            unsafe_allow_html=True,
        )

    with right:
        st.subheader("Quickstart")
        st.code(
            "git clone https://github.com/themanoj-025/FinSight-Agent\n"
            "cd FinSight-Agent\nmake setup && make run",
            language="bash",
        )
        st.markdown(
            "`make run` generates the data, trains and benchmarks the models, and launches the "
            "app — zero manual steps, no data downloads."
        )
        st.subheader("The 6 models being compared")
        st.markdown(
            "- **Logistic Regression** — interpretable baseline\n"
            "- **Random Forest** — tree ensemble\n"
            "- **Gradient Boosting (LightGBM)** — the usual winner\n"
            "- **SGD (linear SVM)** — large-margin view\n"
            "- **Isolation Forest** — unsupervised anomaly baseline\n"
            "- **MLP autoencoder** — reconstruction-error baseline\n\n"
            "Selected by **PR-AUC** (not accuracy): with ~2% fraud, accuracy is meaningless."
        )

    st.divider()
    st.markdown(
        "Explore from the sidebar: **Dashboard**, **Transactions**, **Fraud & Anomaly "
        "Detection** (the model comparison + live risk scan), **Ask the Agent**, **Reports**, "
        "and **Settings** (API key, theme, data regeneration)."
    )


if __name__ == "__main__":
    common.run_render(render)
