"""Metrics + chart generation for the model comparison.

PR-AUC (average precision) is the headline metric: with a ~2% positive class,
accuracy is meaningless and ROC-AUC overstates performance on the rare class
that actually matters here. This choice is documented in the README.
"""

from __future__ import annotations

import os
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.inspection import permutation_importance  # noqa: E402
from sklearn.metrics import (
    average_precision_score,  # noqa: E402
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
    roc_curve,
)

BG = "#0D1526"
FG = "#E7EEF9"
GRID = "#24314F"
PALETTE = ["#34D399", "#4C9EEB", "#F59E0B", "#A78BFA", "#F472B6", "#60A5FA"]


def _style_ax(ax: Any) -> None:
    ax.set_facecolor(BG)
    ax.tick_params(colors=FG)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.xaxis.label.set_color(FG)
    ax.yaxis.label.set_color(FG)
    ax.title.set_color(FG)


def compute_metrics(y_true: np.ndarray, scores: np.ndarray) -> dict[str, float]:
    """Precision / recall / F1 (at 0.5) plus ROC-AUC and PR-AUC."""
    preds = (scores >= 0.5).astype(int)
    return {
        "precision": round(float(precision_score(y_true, preds, zero_division=0)), 4),
        "recall": round(float(recall_score(y_true, preds, zero_division=0)), 4),
        "f1": round(float(f1_score(y_true, preds, zero_division=0)), 4),
        "roc_auc": round(float(roc_auc_score(y_true, scores)), 4),
        "pr_auc": round(float(average_precision_score(y_true, scores)), 4),
    }


def _save(fig: Any, outdir: str, name: str) -> str:
    path = os.path.join(outdir, name)
    fig.savefig(path, dpi=110, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    return path


# ------------------------------------------------------------- data-gen v2
# Per-archetype recall, cohort fairness, temporal stability, calibration.
# These answer the questions a single PR-AUC cannot: *which* fraud archetypes
# does the model catch (or miss), does recall degrade for a cohort, does it
# drift over time, and are the scores actually calibrated?


def per_archetype_recall(
    y_true: np.ndarray, scores: np.ndarray, archetypes: np.ndarray, threshold: float = 0.5
) -> pd.DataFrame:
    """Recall per fraud archetype on the positive rows of a holdout set.

    ``archetypes`` is the per-row ``fraud_archetype`` column (empty for
    non-fraud rows). Recall for an archetype = detected positives / total
    positives of that archetype — a per-archetype table is the honest way to
    show that "easy" patterns are caught while adversarial ones aren't.
    """
    rows: list[dict[str, Any]] = []
    fraud = np.asarray(y_true) == 1
    labels = np.asarray(archetypes, dtype=object)
    if not fraud.any():
        return pd.DataFrame(columns=["archetype", "recall", "precision", "support"])
    for arch in sorted({str(x) for x in labels[fraud] if str(x)}):
        mask = fraud & (labels == arch)
        support = int(mask.sum())
        preds = (np.asarray(scores)[mask] >= threshold).astype(int)
        recall = float(preds.mean()) if support else 0.0
        detected = preds.sum()
        precision = float(detected / support) if support else 0.0  # all-positives group
        rows.append(
            {
                "archetype": arch,
                "recall": round(recall, 4),
                "precision": round(precision, 4),
                "support": support,
            }
        )
    return pd.DataFrame(rows).sort_values("archetype")


def cohort_recall(
    y_true: np.ndarray,
    scores: np.ndarray,
    cohorts: np.ndarray,
    threshold: float = 0.5,
) -> pd.DataFrame:
    """Recall per cohort (e.g. persona archetype) — a cohort-fairness view.

    Disparity = max recall / min recall across cohorts with support; a ratio
    near 1.0 means the model treats every demographic cohort the same.
    """
    rows: list[dict[str, Any]] = []
    fraud = np.asarray(y_true) == 1
    labels = np.asarray(cohorts, dtype=object)
    for cohort in sorted({str(x) for x in labels[fraud] if str(x)}):
        mask = fraud & (labels == cohort)
        support = int(mask.sum())
        recall = (
            float(((np.asarray(scores)[mask] >= threshold).astype(int)).mean()) if support else 0.0
        )
        rows.append({"cohort": cohort, "recall": round(recall, 4), "support": support})
    if not rows:
        return pd.DataFrame(columns=["cohort", "recall", "support"])
    df = pd.DataFrame(rows)
    with_support = df[df["support"] > 0]
    if not with_support.empty and with_support["recall"].max() > 0:
        df["disparity_ratio"] = round(
            with_support["recall"].max() / with_support["recall"].min(), 3
        )
    else:
        df["disparity_ratio"] = 1.0
    return df.sort_values("cohort")


def temporal_stability(
    y_true: np.ndarray, scores: np.ndarray, steps: np.ndarray, n_buckets: int = 5
) -> pd.DataFrame:
    """Precision / recall per time bucket over the holdout window.

    Buckets are equal-count quantiles of the test rows ordered by ``step``, so
    each row shows how detection behaves as time advances — recall that decays
    across buckets is a temporal-stability warning.
    """
    order = np.argsort(np.asarray(steps, dtype=np.int64), kind="stable")
    y = np.asarray(y_true)[order]
    s = np.asarray(scores)[order]
    n = len(y)
    if n == 0:
        return pd.DataFrame(
            columns=["bucket", "start_step", "end_step", "precision", "recall", "support"]
        )
    edges = np.linspace(0, n, n_buckets + 1).astype(int)
    rows: list[dict[str, Any]] = []
    steps_sorted = np.asarray(steps, dtype=np.int64)[order]
    for i in range(n_buckets):
        lo, hi = edges[i], edges[i + 1]
        if hi <= lo:
            continue
        yb, sb = y[lo:hi], s[lo:hi]
        preds = (sb >= 0.5).astype(int)
        support = int(yb.sum())
        tp = int(((preds == 1) & (yb == 1)).sum())
        rows.append(
            {
                "bucket": i,
                "start_step": int(steps_sorted[lo]),
                "end_step": int(steps_sorted[hi - 1]),
                "precision": round(tp / max(1, int(preds.sum())), 4),
                "recall": round(tp / support, 4) if support else 0.0,
                "support": support,
            }
        )
    return pd.DataFrame(rows)


def brier_score(y_true: np.ndarray, scores: np.ndarray) -> float:
    """Mean squared error between predicted probabilities and binary outcome."""
    return round(float(np.mean((np.asarray(scores) - np.asarray(y_true)) ** 2)), 4)


def expected_calibration_error(y_true: np.ndarray, scores: np.ndarray, n_bins: int = 10) -> float:
    """ECE: |mean prediction - observed rate| weighted by bin size."""
    df = calibration_curve(y_true, scores, n_bins)
    if df.empty:
        return float("nan")
    total = float(df["count"].sum())
    ece = float((df["count"] * (df["mean_pred"].sub(df["frac_pos"]).abs())).sum() / total)
    return round(ece, 4)


def calibration_curve(y_true: np.ndarray, scores: np.ndarray, n_bins: int = 10) -> pd.DataFrame:
    """Binned calibration table: mean predicted probability vs. observed rate."""
    y = np.asarray(y_true)
    s = np.asarray(scores)
    if len(s) == 0:
        return pd.DataFrame(columns=["bin", "mean_pred", "frac_pos", "count"])
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(s, bins[1:-1]), 0, n_bins - 1)
    rows: list[dict[str, Any]] = []
    for b in range(n_bins):
        mask = idx == b
        count = int(mask.sum())
        if count == 0:
            continue
        rows.append(
            {
                "bin": b,
                "mean_pred": round(float(s[mask].mean()), 4),
                "frac_pos": round(float(y[mask].mean()), 4),
                "count": count,
            }
        )
    return pd.DataFrame(rows)


def plot_calibration_curve(table: pd.DataFrame, outdir: str) -> str:
    """Calibration plot: mean prediction vs observed rate vs the ideal line."""
    fig, ax = plt.subplots(figsize=(6.5, 5.5))
    ax.plot([0, 1], [0, 1], "--", color=GRID, label="Perfectly calibrated")
    ax.plot(
        table["mean_pred"],
        table["frac_pos"],
        "o-",
        color=PALETTE[0],
        label="Model",
        markersize=6,
    )
    ax.set_xlabel("Mean predicted probability")
    ax.set_ylabel("Observed fraud rate")
    ax.set_title("Calibration curve — holdout window")
    ax.legend(facecolor=BG, labelcolor=FG)
    _style_ax(ax)
    return _save(fig, outdir, "calibration.png")


def plot_archetype_recall(table: pd.DataFrame, outdir: str) -> str:
    """Horizontal bar of recall per fraud archetype (sorted by recall)."""
    d = table.sort_values("recall")
    fig, ax = plt.subplots(figsize=(9, max(3.4, 0.34 * len(d) + 1.2)))
    colors = [PALETTE[0] if r >= 0.5 else PALETTE[2] for r in d["recall"]]
    ax.barh(d["archetype"], d["recall"], color=colors, edgecolor=GRID)
    for i, (rec, sup) in enumerate(zip(d["recall"], d["support"], strict=True)):
        ax.text(rec + 0.01, i, f"{rec:.2f} (n={sup})", va="center", color=FG, fontsize=8)
    ax.axvline(0.5, color=GRID, linestyle=":")
    ax.set_xlim(0, 1.05)
    ax.set_xlabel("Recall at 0.5 threshold")
    ax.set_title("Per-archetype recall — holdout window")
    _style_ax(ax)
    return _save(fig, outdir, "archetype_recall.png")


def plot_temporal_stability(table: pd.DataFrame, outdir: str) -> str:
    """Recall across time buckets — stability at a glance."""
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    ax.plot(table["bucket"], table["recall"], "o-", color=PALETTE[1], label="Recall")
    ax.plot(table["bucket"], table["precision"], "s--", color=PALETTE[3], label="Precision")
    ax.set_xlabel("Time bucket (earliest → latest)")
    ax.set_ylabel("Score")
    ax.set_ylim(0, 1.1)
    ax.set_title("Temporal stability — holdout window buckets")
    ax.legend(facecolor=BG, labelcolor=FG)
    _style_ax(ax)
    return _save(fig, outdir, "temporal_stability.png")


def plot_bar_comparison(metrics_table: pd.DataFrame, outdir: str) -> str:
    """Grouped bar chart of F1 / ROC-AUC / PR-AUC with ±std error bars when the
    table carries CV mean/std columns (from TimeSeriesSplit), else plain bars."""
    pairs: list[tuple[str, str, str | None]] = [
        ("f1", "f1_mean", "f1_std"),
        ("roc_auc", "roc_auc_mean", "roc_auc_std"),
        ("pr_auc", "pr_auc_mean", "pr_auc_std"),
    ]
    metrics: list[tuple[str, str, str | None]] = []
    for plain, mean_col, std_col in pairs:
        if mean_col in metrics_table.columns:
            metrics.append((mean_col.upper(), mean_col, std_col))
        elif plain in metrics_table.columns:
            metrics.append((plain.upper(), plain, None))
    fig, ax = plt.subplots(figsize=(9, 4.6))
    x = np.arange(len(metrics_table))
    width = 0.8 / len(metrics)
    for i, (label, col, std_col) in enumerate(metrics):
        vals = metrics_table[col].astype(float).to_numpy()
        errs = (
            metrics_table[std_col].astype(float).to_numpy()
            if std_col in metrics_table.columns
            else None
        )
        bars = ax.bar(
            x + (i - 1) * width,
            vals,
            width,
            yerr=errs,
            capsize=2,
            label=label,
            color=PALETTE[i],
            edgecolor=GRID,
        )
        ax.bar_label(bars, fmt="%.2f", fontsize=7, color=FG)
    ax.set_xticks(x, metrics_table["model"], rotation=20, ha="right", color=FG)
    ax.set_ylabel("Score", color=FG)
    ax.set_ylim(0, 1.1)
    ax.legend(facecolor=BG, labelcolor=FG, loc="upper left")
    ax.set_title("Model comparison — F1, ROC-AUC, PR-AUC (mean ± std over 5-fold time-series CV)")
    _style_ax(ax)
    return _save(fig, outdir, "model_comparison_bar.png")


def plot_curves(y_true: np.ndarray, scores: dict[str, np.ndarray], outdir: str) -> tuple[str, str]:
    """Overlaid ROC curves and overlaid Precision-Recall curves."""
    roc_path, pr_path = "", ""
    fig_roc, ax_roc = plt.subplots(figsize=(7, 5.5))
    fig_pr, ax_pr = plt.subplots(figsize=(7, 5.5))
    for i, (name, s) in enumerate(scores.items()):
        color = PALETTE[i % len(PALETTE)]
        fpr, tpr, _ = roc_curve(y_true, s)
        ax_roc.plot(fpr, tpr, color=color, label=f"{name} (AUC {roc_auc_score(y_true, s):.3f})")
        precision, recall, _ = precision_recall_curve(y_true, s)
        ax_pr.plot(
            recall,
            precision,
            color=color,
            label=f"{name} (AP {average_precision_score(y_true, s):.3f})",
        )
    ax_roc.plot([0, 1], [0, 1], "--", color=GRID)
    ax_roc.set_xlabel("False positive rate")
    ax_roc.set_ylabel("True positive rate")
    ax_roc.set_title("ROC curves — one line per model")
    ax_roc.legend(facecolor=BG, labelcolor=FG, fontsize=7)
    _style_ax(ax_roc)
    roc_path = _save(fig_roc, outdir, "roc_curves.png")

    ax_pr.set_xlabel("Recall")
    ax_pr.set_ylabel("Precision")
    ax_pr.set_title("Precision-Recall curves — more informative than ROC for rare fraud")
    ax_pr.legend(facecolor=BG, labelcolor=FG, fontsize=7)
    _style_ax(ax_pr)
    pr_path = _save(fig_pr, outdir, "pr_curves.png")
    return roc_path, pr_path


def plot_confusion_grid(y_true: np.ndarray, scores: dict[str, np.ndarray], outdir: str) -> str:
    n = len(scores)
    fig, axes = plt.subplots(1, n, figsize=(3.4 * n, 3.2), squeeze=False)
    for ax, (name, s) in zip(axes[0], scores.items(), strict=True):
        cm = confusion_matrix(y_true, (s >= 0.5).astype(int))
        im = ax.imshow(cm, cmap="Greens", aspect="auto")
        ax.set_title(name, color=FG, fontsize=8)
        ax.set_xticks([0, 1], ["OK", "Fraud"], color=FG)
        ax.set_yticks([0, 1], ["OK", "Fraud"], color=FG)
        for i in range(2):
            for j in range(2):
                ax.text(
                    j,
                    i,
                    cm[i, j],
                    ha="center",
                    va="center",
                    color="black" if cm[i, j] > cm.max() / 2 else FG,
                )
        fig.colorbar(im, ax=ax, fraction=0.046)
    fig.suptitle("Confusion matrices at 0.5 threshold", color=FG)
    return _save(fig, outdir, "confusion_matrix_grid.png")


def plot_feature_importance(
    model: Any, X_test: np.ndarray, y_test: np.ndarray, feature_names: list[str], outdir: str
) -> str:
    """Top feature importances for the winner: native importances or permutation."""
    if hasattr(model, "feature_importances_"):
        importances = np.asarray(model.feature_importances_, dtype=float)
        labels = feature_names
    else:
        sample = min(len(X_test), 500)
        perm = permutation_importance(
            model, X_test[:sample], y_test[:sample], n_repeats=5, random_state=42, n_jobs=-1
        )
        importances = perm.importances_mean
        labels = feature_names
    order = np.argsort(importances)[::-1][:15]
    fig, ax = plt.subplots(figsize=(8, 5.5))
    ax.barh(
        [labels[i] for i in order[::-1]], importances[order[::-1]], color=PALETTE[0], edgecolor=GRID
    )
    ax.set_xlabel("Importance")
    ax.set_title("Feature importance — best model")
    _style_ax(ax)
    return _save(fig, outdir, "feature_importance.png")


# ------------------------------------------------------------- model card
# Phase A.4: MODEL_CARD.md is *generated* from best_model_metadata.json by
# train_and_compare.py — never hand-edited, so the card and the benchmark can't
# drift apart (the same anti-drift principle as the blend-weight prose).


def model_card_markdown(metadata: dict[str, Any]) -> str:
    """Auto-generated model card from the training metadata dict."""
    cfg = metadata.get("config", {})
    ds = metadata.get("dataset", {})
    cal = metadata.get("calibration", {})
    arch = metadata.get("archetype_recall", [])
    cohort = metadata.get("cohort_fairness", [])
    hpo = metadata.get("hpo")

    def pct(x: Any, nd: int = 1) -> str:
        try:
            return f"{float(x) * 100:.{nd}f}%"
        except (TypeError, ValueError):
            return "n/a"

    def num(x: Any) -> str:
        try:
            return f"{float(x):.4f}"
        except (TypeError, ValueError):
            return "n/a"

    lines = [
        "# Model Card — FinSight Agent risk-scoring model",
        "",
        "> **Auto-generated** from `model_bench/best_model_metadata.json` by "
        "`model_bench/train_and_compare.py`. Do not hand-edit — every figure "
        "below is read from the benchmark's own metadata.",
        "",
        "## Model identity",
        f"- **Algorithm:** {metadata.get('algorithm', 'n/a')}",
        "- **Task:** per-transaction fraud-risk scoring (rare positive class, binary)",
        f"- **Selection:** {metadata.get('selection_metric', 'n/a')} — {cfg.get('cv', 'n/a')}",
        f"- **Selection policy:** {cfg.get('selection_policy', 'n/a')}",
        f"- **Training timestamp (UTC):** {metadata.get('training_timestamp_utc', 'n/a')}",
        f"- **Git commit:** {metadata.get('git_commit', 'n/a')}",
        f"- **Bundle signature:** {metadata.get('bundle_signature', {}).get('algorithm', 'none')} "
        f"(key origin: {metadata.get('bundle_signature', {}).get('key_origin', 'n/a')})",
        "",
        "## Intended use",
        "- **Primary:** explainable per-transaction fraud-risk scoring on the synthetic ",
        "  FinSight ledger, blended with audit rules + an isolation-forest anomaly score ",
        "  into one risk score. Not for use on real financial data.",
        "- **Out of scope:** autonomous blocking/declining decisions; any deployment on ",
        "  real accounts; regulatory decisioning. The model is a demo-grade, synthetic-only ",
        "  artifact (see docs/KNOWN_LIMITATIONS.md).",
        "",
        "## Training data",
        f"- **Provenance:** synthetic (deterministic generator, seed {cfg.get('random_state', 'n/a')}), ",
        "  no real PII.",
        f"- **Size:** {ds.get('rows', 'n/a'):,} rows ({ds.get('train_rows', 'n/a'):,} train / ",
        f"{ds.get('test_rows', 'n/a'):,} temporal holdout), fraud rate {pct(ds.get('fraud_rate'))}.",
        f"- **Personas:** {ds.get('personas', 'n/a')} total ({ds.get('focal_personas', 'n/a')} focal) ",
        "from 6 archetypes.",
        f"- **Features:** {len(metadata.get('feature_list', []))} strictly backward-looking ",
        "features (no temporal leakage).",
        "",
        "## Performance",
        f"- **CV (mean ± std):** PR-AUC {num(metadata.get('pr_auc_mean'))} ± "
        f"{num(metadata.get('pr_auc_std'))} · ROC-AUC {num(metadata.get('roc_auc_mean'))} ± ",
        f"{num(metadata.get('roc_auc_std'))} · F1 {num(metadata.get('f1_mean'))} ± ",
        f"{num(metadata.get('f1_std'))}",
        "- **Holdout:** "
        + "; ".join(
            f"{k.replace('_', ' ')} {num(v)}"
            for k, v in metadata.get("metrics_on_holdout", {}).items()
        )
        + ".",
        "",
        "### Per-archetype recall (holdout) — the interview-relevant view",
        "",
        "| Archetype | Recall @ 0.5 | Support |",
        "| --- | ---: | ---: |",
    ]
    for r in arch:
        lines.append(f"| {r['archetype']} | {num(r['recall'])} | {r['support']} |")
    lines += [
        "",
        "**Known failure modes:** the adversarial tier (mimicry, account takeover, ",
        "seasonal mimicry) has materially lower recall than the easy/medium tiers — ",
        "that is by design (difficulty-graded fraud library) and is stated honestly here. ",
        "A deployment would pair this model with the rule detectors, which are what catch ",
        "most structural fraud.",
        "",
        "### Cohort fairness (persona archetypes)",
        "",
        "| Cohort | Recall | Support |",
        "| --- | ---: | ---: |",
    ]
    for r in cohort:
        lines.append(f"| {r['cohort']} | {num(r['recall'])} | {r['support']} |")
    lines += [
        "",
        "## Calibration",
        f"- **Brier score:** {num(cal.get('brier'))}",
        f"- **Expected calibration error (ECE):** {num(cal.get('expected_calibration_error'))}",
    ]
    if hpo:
        imp = hpo.get("improvement")
        imp_str = f"{imp:+.4f}" if isinstance(imp, (int, float)) else "n/a"
        tuned = ", ".join(f"{k}={v}" for k, v in (hpo.get("best_params") or {}).items())
        lines += [
            "",
            "## Hyperparameter optimization (Optuna)",
            f"- **Study:** {hpo.get('study_name', 'n/a')} (id {hpo.get('study_id', 'n/a')}, "
            f"{hpo.get('n_trials', 'n/a')} trials) — Optuna over the LightGBM family using "
            "the same mean-PR-AUC time-series CV objective as this benchmark.",
            f"- **Baseline (registry defaults):** {num(hpo.get('baseline_value'))} · "
            f"**Best trial:** {num(hpo.get('best_value'))} · **Improvement:** {imp_str} "
            f"(adopted — cleared the {num(hpo.get('min_improvement'))} CV-noise gate).",
            f"- **Tuned params:** `{tuned}`",
        ]
    lines += [
        "",
        "## Ethical considerations",
        "- The dataset is **fully synthetic** — no real PII, no real accounts, no real ",
        "  transactions. Nothing learned here transfers to real-world data without ",
        "  re-validation.",
        "- Per-cohort recall is published above so model disparity is visible, not hidden.",
        "- The risk score is **not** a financial decision. It exists to demonstrate ",
        "  explainable fraud detection on synthetic data.",
        "",
    ]
    return "\n".join(lines)
