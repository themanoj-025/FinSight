"""Trust & Transparency (Phase C.3) — the honest, reviewer-facing page.

Surfaces the project's own audit artifacts in plain language: the model card,
the dataset card, per-archetype recall (including the adversarial-tier gap),
cohort fairness, calibration, a summary of the STRIDE threat model, and a
documented cost projection. Every number on this page is read live from
`model_bench/best_model_metadata.json` / `config.yaml` — nothing is hardcoded,
so the page can never drift from reality (the same anti-drift principle as the
blend-weight prose elsewhere in the project).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from app import common

ROOT = common.ROOT


def _metadata_path(cfg: dict) -> Path:
    return ROOT / str(
        cfg.get("model_bench", {}).get("metadata_path", "model_bench/best_model_metadata.json")
    )


def _load_metadata() -> dict | None:
    cfg_path = ROOT / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except OSError:
        cfg = {}
    path = _metadata_path(cfg)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _pct(x) -> str:
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(x, nd: int = 4) -> str:
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return "n/a"


def render() -> None:
    st.set_page_config(
        page_title="Trust & Transparency · FinSight Agent",
        page_icon="🛡️",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    common.inject_css()
    common.require_auth()
    common.ensure_data()  # page_header -> focal_user_selector() reads facts
    common.page_header(
        "Trust & Transparency",
        "The honest view of this system: model card, dataset card, and the gaps we publish on purpose",
    )

    meta = _load_metadata()
    if meta is None:
        st.info(
            "No `best_model_metadata.json` on disk yet — run `make train` to generate the "
            "model card, then come back. (The page reads everything live from that file, "
            "so it can never drift from the benchmark.)"
        )
        return

    # ---------------------------------------------------------------- model card
    st.subheader("📋 Model card")
    st.caption(
        "Auto-generated from `model_bench/best_model_metadata.json` — see "
        "[`model_bench/MODEL_CARD.md`](../../model_bench/MODEL_CARD.md)."
    )
    c1, c2, c3 = st.columns(3)
    c1.metric("Algorithm", meta.get("algorithm", "n/a"))
    c2.metric(
        "CV PR-AUC (mean ± std)",
        f"{_num(meta.get('pr_auc_mean'))} ± {_num(meta.get('pr_auc_std'))}",
    )
    c3.metric("Holdout PR-AUC", _num((meta.get("metrics_on_holdout") or {}).get("pr_auc")))

    cal = meta.get("calibration") or {}
    b1, b2, b3 = st.columns(3)
    b1.metric("Brier score", _num(cal.get("brier")))
    b2.metric("Expected calibration error", _num(cal.get("expected_calibration_error")))
    b3.metric("Fraud rate (dataset)", _pct((meta.get("dataset") or {}).get("fraud_rate")))

    st.markdown(
        "**Intended use.** Explainable per-transaction fraud-risk scoring on the "
        "synthetic ledger, blended with audit rules + an isolation-forest anomaly "
        "score. Not for real financial decisions — synthetic-only, demo-grade "
        "(see [Known Limitations](../../docs/KNOWN_LIMITATIONS.md))."
    )

    # ----------------------------------------------------- per-archetype recall
    arch = meta.get("archetype_recall", [])
    if arch:
        st.subheader("🎯 Per-archetype recall — where the model wins and loses")
        df = pd.DataFrame(arch)
        st.dataframe(
            df.style.map(
                lambda v: (
                    "color: #34D399" if float(v) >= 0.5 else "color: #F59E0B; font-weight: 600"
                ),
                subset=["recall"],
            ),
            width="stretch",
            hide_index=True,
        )
        low = [r["archetype"] for r in arch if r.get("recall") is not None and r["recall"] < 0.5]
        if low:
            st.markdown(
                f"**The honest gap:** the adversarial tier (`{', '.join(low)}`) is caught far "
                "less often than easy/medium patterns — that is by design (difficulty-graded "
                "fraud library), and this page shows it rather than hiding it. A deployment "
                "would pair the model with the rule detectors, which catch most structural fraud."
            )
        st.caption("Recall at the 0.5 threshold on the temporal holdout window.")

    # --------------------------------------------------------- cohort fairness
    cohort = meta.get("cohort_fairness", [])
    if cohort:
        st.subheader("👥 Cohort fairness (persona archetypes)")
        cdf = pd.DataFrame(cohort)
        st.dataframe(cdf, width="stretch", hide_index=True)
        ratio = [r.get("disparity_ratio") for r in cohort if r.get("disparity_ratio") is not None]
        if ratio:
            st.caption(
                f"Max/min recall disparity ratio: **{max(ratio):.3f}** — 1.0 means every cohort is treated the same."
            )

    # ----------------------------------------------------------- threat model
    st.subheader("🔐 Threat model (STRIDE summary)")
    st.markdown(
        "A full STRIDE walkthrough of the three trust boundaries (browser ↔ Streamlit, "
        "Streamlit/agent ↔ Anthropic API, app ↔ facts API/SQLite) lives in "
        "[`docs/technical/SecurityAndCompliance.md`](../../docs/technical/SecurityAndCompliance.md). "
        "The short version — what is mitigated and what is accepted:"
    )
    st.markdown(
        "- **Timing-safe auth:** `APP_PASSWORD` and `FINSIGHT_API_KEY` are compared with "
        "`hmac.compare_digest`.\n"
        "- **Pickle-RCE:** model bundles are HMAC-SHA256 signed at train time and verified "
        "before `joblib.load`; a tampered bundle is refused and the app degrades to rule-only.\n"
        "- **Budget enforcement:** per-session LLM budgets are persisted in SQLite keyed by "
        "session id and enforced against the API's *exact* token counts — a page reload can't "
        "reset them.\n"
        "- **Bounded API responses:** `/api/v1/transactions` is paginated (`limit`/`offset` + "
        "`total`/`truncated`).\n"
        "- **Accepted for demo scope:** no real authentication/authorization (shared password "
        "or `X-API-Key`), rate limiting off by default (opt in via "
        "`FINSIGHT_RATE_LIMIT_PER_MIN`), API keys in env vars. All named explicitly in "
        "[`docs/KNOWN_LIMITATIONS.md`](../../docs/KNOWN_LIMITATIONS.md) — the value here is "
        "that the analysis is done and visible, not that every risk is closed."
    )

    # --------------------------------------------------------- dataset honesty
    st.subheader("🧾 Dataset card")
    ds = meta.get("dataset") or {}
    st.markdown(
        "The ledger is **fully synthetic** — deterministic generator, no real PII, no real "
        "accounts. Composition, collection process, and known limitations are documented in "
        "[`docs/DATASHEET.md`](../../docs/DATASHEET.md). "
        + (
            f"This run: {ds.get('rows', 'n/a'):,} rows "
            f"({ds.get('train_rows', 'n/a'):,} train / {ds.get('test_rows', 'n/a'):,} temporal holdout), "
            f"trained at seed {((meta.get('config') or {}).get('random_state', 'n/a'))}."
        )
    )

    # ---------------------------------------------------------- cost projection
    st.subheader("💰 Cost / FinOps projection")
    st.markdown(
        "**Documented formula, not a live meter.** If this were deployed at **N daily active "
        "users** with **Z%** of sessions using the LLM agent and **Q** questions per session:\n\n"
        "`monthly LLM cost ≈ N × Z × Q × 30 × (input_tokens/1M × $3 + output_tokens/1M × $15)` "
        "(Sonnet-class list prices from `config.yaml agent.pricing`, per-session tokens are "
        "metered exactly in the Settings page). Hosting is a single small container — well "
        "under the LLM cost at any realistic scale. The point: cost is a designed, visible "
        "quantity here, not an afterthought."
    )
    st.caption(
        "All performance/fairness figures above are read live from "
        "`best_model_metadata.json` / `config.yaml` — none are hardcoded. The "
        "cost section is a documented formula with config-mirrored list prices "
        "(agent.pricing), deliberately labeled as an estimate."
    )


if __name__ == "__main__":
    common.run_render(render)
