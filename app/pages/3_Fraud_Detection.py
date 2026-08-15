"""Fraud & Anomaly Detection — the model comparison artifacts rendered live,
plus a live risk-scored transaction table with adjustable sensitivity."""

import pandas as pd
import streamlit as st

from app import common

RESULTS = common.ROOT / "model_bench" / "results"
CHARTS = {
    "PR-AUC · ROC-AUC · F1": "model_comparison_bar.png",
    "ROC curves": "roc_curves.png",
    "Precision-Recall curves": "pr_curves.png",
    "Confusion matrices": "confusion_matrix_grid.png",
    "Feature importance": "feature_importance.png",
    "Per-archetype recall": "archetype_recall.png",
    "Temporal stability": "temporal_stability.png",
    "Calibration": "calibration.png",
    "HPO parameter importance": "hpo_param_importance.png",  # appears after `make hpo`
}


def _metrics_table() -> pd.DataFrame | None:
    path = RESULTS / "metrics_table.csv"
    if not path.exists():
        return None
    return pd.read_csv(path).round(4)


def _model_comparison_tab() -> None:
    st.markdown(
        "Models are compared with a **temporal train/test split** (no shuffle) on "
        "**strictly backward-looking features**, selected by **mean PR-AUC over 5-fold "
        "time-series cross-validation** — with ~2% fraud, accuracy is meaningless and "
        "ROC-AUC overstates performance on the class that actually matters. "
        "See `model_bench/best_model_metadata.json` for mean ± std."
    )
    table = _metrics_table()
    if table is None:
        st.info("Models not trained yet — run the scan below, or `make train`.")
        return
    col_img, col_tab = st.columns([3, 2], gap="large")
    with col_img:
        names, paths = zip(*CHARTS.items(), strict=True)
        sel = st.radio("Chart", names, horizontal=True, label_visibility="collapsed")
        img = RESULTS / paths[names.index(sel)]
        if img.exists():
            st.image(str(img), width="stretch", caption=sel)
    with col_tab:
        st.dataframe(table, width="stretch", hide_index=True)
        st.caption(
            "Winner (best PR-AUC) is refit on all data and serialized to "
            "`best_model.joblib` + `risk_model_bundle.joblib`, with metadata in "
            "`best_model_metadata.json`."
        )


def _style_risk(df: pd.DataFrame, thr: float) -> pd.DataFrame.style:
    def color(v: float) -> str:
        # risk_score is always a numeric column; NaN compares False and is left
        # unstyled (identical behavior to the old float(v) fallback).
        return "color: #EF4444; font-weight:700" if v >= thr else ""

    return df.style.map(color, subset=["risk_score"]).format(
        {"amount": "${:,.2f}", "risk_score": "{:.2f}"}
    )


def _shap_explainer(rows: list[dict]) -> None:
    """Per-transaction SHAP explanation (Phase 6 stretch feature)."""
    import plotly.graph_objects as go

    explained = [r for r in rows if r.get("explanation")]
    if not explained:
        return
    st.markdown(
        "### 🔍 Why was this flagged? — SHAP feature contributions"
        "<span class='pill'>TreeSHAP (LightGBM pred_contrib)</span>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Pick a transaction to see which features pushed the model's fraud "
        "probability up (positive) or down (negative). Positive contributions "
        "drive it toward fraud; negative ones pull it toward normal."
    )
    labels = {
        f"{r['date']} · {r['merchant']} · ${r['amount']:,.2f} · risk {r['risk_score']:.2f}": r
        for r in explained
    }
    choice = st.selectbox("Transaction", list(labels.keys()), key="shap_txn")
    rec = labels[choice]
    expl = rec["explanation"]

    col_chart, col_text = st.columns([3, 2], gap="large")
    with col_chart:
        feats = expl["top_features"]
        df_feats = pd.DataFrame(feats)
        df_feats["abs"] = df_feats["contribution"].abs()
        df_feats = df_feats.sort_values("abs").tail(8)
        colors = ["#EF4444" if c >= 0 else "#34D399" for c in df_feats["contribution"]]
        fig = go.Figure(
            go.Bar(
                x=df_feats["contribution"],
                y=df_feats["feature"],
                orientation="h",
                marker_color=colors,
                text=[f"{c:+.3f}" for c in df_feats["contribution"]],
                textposition="outside",
            )
        )
        fig.update_layout(
            title="Feature contributions to the model score",
            xaxis_title="SHAP value (log-odds shift)",
            margin=dict(t=40, b=10, l=10, r=10),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font_color="#E7EEF9" if common.theme() == "dark" else "#0F172A",
            height=320,
        )
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

    with col_text:
        bias = expl["bias"]
        base_p = expl["base_probability"]
        pos = [f for f in expl["top_features"] if f["contribution"] > 0]
        neg = [f for f in expl["top_features"] if f["contribution"] < 0]
        st.markdown(
            f"<div class='layer'><div class='layer-name'>What drove this score</div>"
            f"<p>The model starts from a base fraud probability of "
            f"<b>{base_p * 100:.1f}%</b> (log-odds {bias:+.2f}). "
            + (
                f"<b>{common.esc(pos[0]['feature'])}</b> pushed it up the most ({pos[0]['contribution']:+.2f})…"
                if pos
                else "No feature pushed it up meaningfully."
            )
            + "</p>"
            + (
                f"<p style='color:{common.DANGER}'>Top upward drivers: "
                + ", ".join(
                    f"{common.esc(f['feature'])} ({f['contribution']:+.2f})" for f in pos[:4]
                )
                + "</p>"
                if pos
                else ""
            )
            + (
                f"<p style='color:{common.ACCENT}'>Top counter-signals: "
                + ", ".join(
                    f"{common.esc(f['feature'])} ({f['contribution']:+.2f})" for f in neg[:4]
                )
                + "</p>"
                if neg
                else ""
            )
            + "</div>",
            unsafe_allow_html=True,
        )
        st.caption(
            f"Explained via {expl['method']}. Contributions are in log-odds "
            "space and sum (with the bias) to the model's raw prediction. "
            "The rule signal is separate — see the `reason` column."
        )


def _similar_transactions_explainer(facts, rows: list[dict]) -> None:
    """Phase B.1 — the "what does this look like?" comparison set.

    Clicking a flagged transaction shows its k nearest neighbours in feature
    space (L2 over the strictly backward-looking features) with their
    fraud-archetype labels, grounding the flag in real, similar cases instead
    of a black-box score. Works in rule-only mode too — retrieval needs the
    feature matrix, not the model bundle.
    """
    with_row = [r for r in rows if r.get("row_index") is not None]
    if not with_row:
        return
    st.markdown("### 🔎 What does this look like? — similar transactions")
    st.caption(
        "Pick a flagged transaction to see its nearest neighbours in feature "
        "space with their fraud-archetype labels. A flag is easier to trust "
        "when you can see what it resembles."
    )
    labels = {
        f"{r['date']} · {r['merchant']} · ${r['amount']:,.2f} · risk {r['risk_score']:.2f}": r
        for r in with_row
    }
    choice = st.selectbox("Transaction", list(labels.keys()), key="similar_txn")
    rec = labels[choice]
    try:
        result = facts.find_similar_transactions(transaction_id=rec["row_index"], k=5)
    except Exception:  # noqa: BLE001 — older API / disabled flag: degrade visibly
        st.info("Similar-transaction retrieval is unavailable on this data source.")
        return
    data = result.get("data", {})
    if not data.get("enabled", True):
        st.info(result.get("summary", "Similar-transaction retrieval is disabled."))
        return
    neighbors = data.get("neighbors", [])
    if not neighbors:
        st.info(result.get("summary", "No similar transactions found."))
        return
    df = pd.DataFrame(neighbors)
    show = [
        c
        for c in (
            "date",
            "merchant",
            "amount",
            "category",
            "type",
            "isFraud",
            "fraud_archetype",
            "distance",
        )
        if c in df.columns
    ]

    def label_color(v: object) -> str:
        if v == "legitimate":
            return "color: #34D399"
        return "color: #EF4444; font-weight: 600"

    st.dataframe(
        df[show]
        .style.map(label_color, subset=["fraud_archetype"])
        .format({"amount": "${:,.2f}", "distance": "{:.4f}"}),
        width="stretch",
        hide_index=True,
        column_config={
            "fraud_archetype": st.column_config.TextColumn("Pattern"),
            "isFraud": st.column_config.CheckboxColumn("Fraud"),
        },
    )
    n_fraud = sum(1 for r in neighbors if r.get("fraud_archetype") != "legitimate")
    st.caption(
        f"{n_fraud} of {len(neighbors)} neighbours are known fraud patterns — "
        f"index backend: {data.get('backend', 'n/a')}. Similarity is feature-space "
        "(not semantic-text), so it stays explainable and leakage-safe."
    )


def _live_scan_tab() -> None:
    facts = common.get_facts()
    default_thr = float(facts.cfg.get("risk", {}).get("fraud_threshold", 0.7))
    c1, c2, c3 = st.columns([1, 2, 1])
    with c1:
        thr = st.slider(
            "Sensitivity threshold",
            0.0,
            1.0,
            default_thr,
            0.05,
            help="Transactions with risk ≥ threshold are flagged.",
        )
    with c2:
        st.caption("")
        # Default to the focal account: this is a personal-finance scan, and the
        # filter is applied *before* head(limit) so toggling it never drops rows.
        focal_only = st.toggle("Focal account only", value=True)
    with c3:
        st.caption("")
        limit = st.slider("Rows shown", 5, 50, 15, 5)

    if facts.rule_only():
        st.warning(
            "⚠️ **Rule-only mode — model unavailable.** No trained model bundle is loaded, "
            "so risk scores are the rule score alone (renormalized to weight 1.0). "
            "Run `make train` to enable the blended score."
        )

    result = facts.risk_scored_transactions(
        limit=limit, threshold=thr, focal_only=focal_only, include_explanations=True
    )
    flagged = result["data"]["flagged_count"]
    total = result["data"]["total_scored"]
    if flagged:
        st.markdown(
            f"### {flagged:,} of {total:,} transactions flagged "
            f"<span class='risk-flag'>({flagged / total * 100:.1f}%)</span>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "### <span class='ok-text'>No anomalies flagged this period — "
            "your account looks clean.</span>",
            unsafe_allow_html=True,
        )

    rows = pd.DataFrame(result["data"]["rows"])
    if rows.empty:
        st.info("Lower the threshold to see more of the risk-ranked table.")
        return
    cols = [
        "date",
        "type",
        "amount",
        "merchant",
        "category",
        "is_focal_user",
        "rule_score",
        "model_score",
        "isolation_score",
        "risk_score",
        "reason",
    ]
    st.dataframe(
        _style_risk(rows[cols].sort_values("risk_score", ascending=False), thr),
        width="stretch",
        hide_index=True,
        column_config={
            "is_focal_user": st.column_config.CheckboxColumn("Focal"),
            "reason": st.column_config.TextColumn("Plain-English reason", width="large"),
        },
    )

    if not facts.rule_only():
        _shap_explainer(rows.to_dict(orient="records"))
    _similar_transactions_explainer(facts, rows.to_dict(orient="records"))

    from finance_agent.tools import blend_description

    st.caption(blend_description(facts.cfg.get("risk", {}), rule_only=facts.rule_only()))


def render() -> None:
    st.set_page_config(
        page_title="Fraud & Anomaly Detection · FinSight Agent",
        page_icon="🛡️",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    common.inject_css()
    common.require_auth()
    common.ensure_data()
    common.ensure_model()

    common.page_header(
        "Fraud & Anomaly Detection", "Model benchmark evidence, plus a live, explainable risk scan"
    )
    tab_compare, tab_scan = st.tabs(["📊 Model comparison", "🛡️ Live risk scan"])
    with tab_compare:
        _model_comparison_tab()
    with tab_scan:
        _live_scan_tab()


if __name__ == "__main__":
    common.run_render(render)
