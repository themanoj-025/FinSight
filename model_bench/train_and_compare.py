"""End-to-end model benchmark.

    python model_bench/train_and_compare.py --data data/transactions.csv --config config.yaml

Evaluation is honest by construction:

  * **Temporal split** — rows are sorted by `step` and split at a fixed
    percentile (first 80% train, last 20% test). No shuffling.
  * **No feature leakage** — `build_features` is strictly backward-looking, so a
    test row's features only reference information available at or before its
    own `step`.
  * **Cross-validation** — model selection uses `TimeSeriesSplit(k=5)` on the
    train portion and reports mean ± std, so `best_model_metadata.json` never
    again carries a single-split PR-AUC of 1.000.

The winner is refit on all training data, serialized as `best_model.joblib`
plus `risk_model_bundle.joblib` (winner + IsolationForest + scaler), with
metadata in `best_model_metadata.json`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent)
)  # allow `python model_bench/train_and_compare.py`

from typing import Any

import joblib
import numpy as np
import pandas as pd
import yaml
from sklearn.base import clone
from sklearn.model_selection import TimeSeriesSplit
from sklearn.neural_network import MLPRegressor
from sklearn.preprocessing import StandardScaler

from finance_agent.bundle_security import ALGORITHM, key_origin, write_signature
from finance_agent.features import build_features
from model_bench import evaluate, hpo, models

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("model_bench")

CV_FOLDS = 5


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _fold_metrics(y_true: np.ndarray, scores: np.ndarray) -> dict[str, float] | None:
    """Compute metrics for one CV fold, or None if the fold has a single class."""
    if len(np.unique(y_true)) < 2:
        return None
    return evaluate.compute_metrics(y_true, scores)


def hpo_provenance(hpo_best: dict[str, Any] | None) -> tuple[int | None, dict[str, Any] | None]:
    """Metadata (``hpo_study_id``, ``hpo`` block) from an adopted HPO record.

    Phase A.1 acceptance: when a tuned model is promoted (``hpo_best.json``
    with ``adopted: true``), ``best_model_metadata.json`` records the study
    provenance. Returns ``(None, None)`` otherwise, so the metadata is
    explicit about having no HPO lineage rather than omitting the key.
    """
    if not hpo_best or not hpo_best.get("adopted"):
        return None, None
    block = {
        "study_id": int(hpo_best["study_id"]),
        "study_name": hpo_best["study_name"],
        "n_trials": int(hpo_best["n_trials"]),
        "baseline_value": float(hpo_best["baseline_value"]),
        "best_value": float(hpo_best["best_value"]),
        "improvement": float(hpo_best["improvement"]),
        "min_improvement": float(hpo_best["min_improvement"]),
        "best_params": dict(hpo_best["best_params"]),
    }
    return int(hpo_best["study_id"]), block


def _aggregate(rows: list[dict[str, float] | None], key: str) -> tuple[float, float]:
    vals = [float(r[key]) for r in rows if r is not None and key in r]
    if not vals:
        return float("nan"), float("nan")
    return round(float(np.mean(vals)), 4), round(float(np.std(vals)), 4)


def main() -> None:
    parser = argparse.ArgumentParser(description="Train and compare fraud-detection models.")
    parser.add_argument("--data", default="data/transactions.csv")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--outdir", default=None)
    parser.add_argument(
        "--hpo",
        action="store_true",
        help="run the Optuna HPO study over the LightGBM family and exit — "
        "opt-in, never part of a retrain (make hpo)",
    )
    parser.add_argument(
        "--n-trials",
        type=int,
        default=None,
        help="override model_bench.hpo.n_trials (--hpo only)",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help="with --hpo: adopt the tuned params if they clear "
        "model_bench.hpo.min_improvement (the documented human review step)",
    )
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    bench_cfg = cfg.get("model_bench", {})
    rs = int(bench_cfg.get("random_state", 42))
    test_size = float(bench_cfg.get("test_size", 0.25))
    selection_metric = str(bench_cfg.get("selection_metric", "pr_auc"))
    # Large-ledger training cap: when the temporal train window exceeds
    # `max_train_rows`, draw a stratified (by fraud class) sample so the
    # 6-model TimeSeriesSplit CV stays practical on multi-million-row bench
    # ledgers. The temporal test window is never sampled — evaluation always
    # runs on the full holdout. 0 / absent = no cap (tiny/demo behave exactly
    # as before).
    max_train_rows = int(bench_cfg.get("max_train_rows", 0) or 0)
    outdir = args.outdir or str(bench_cfg.get("artifacts_dir", "model_bench/results"))
    best_path = str(bench_cfg.get("best_model_path", "model_bench/best_model.joblib"))
    bundle_path = str(bench_cfg.get("bundle_path", "model_bench/risk_model_bundle.joblib"))
    metadata_path = str(bench_cfg.get("metadata_path", "model_bench/best_model_metadata.json"))
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(os.path.dirname(best_path) or ".", exist_ok=True)

    # Format-aware load: the bench tier writes Parquet; tiny/demo write CSV.
    if str(args.data).lower().endswith((".parquet", ".pq")):
        # `dtype_backend="pyarrow"` keeps string columns arrow-backed instead of
        # materializing Python str objects: the 10.7M-row bench ledger would
        # otherwise need ~10GB of RAM to load into pandas (object columns hold
        # one Python str per cell). Arrow-backed strings stay compact and work
        # with every downstream op (groupby/get_dummies/Categorical/isin); the
        # numeric/bool columns are converted back to native numpy dtypes
        # because arrow-backed numerics reject some ops `build_features` uses
        # (e.g. ``step % 24``, ``np.maximum`` on balances).
        df = pd.read_parquet(args.data, dtype_backend="pyarrow")
        for _col in df.columns:
            _dt = df[_col].dtype
            if isinstance(_dt, pd.ArrowDtype):
                _nd = _dt.numpy_dtype
                if np.issubdtype(_nd, np.number) or np.issubdtype(_nd, np.bool_):
                    df[_col] = df[_col].astype(_nd)
        log.info("Loaded Parquet ledger: %s", args.data)
    else:
        df = pd.read_csv(args.data)
    y_all = df["isFraud"].astype(int).to_numpy()

    # --- Temporal split: first (1 - test_size) of steps = train, rest = test.
    # Stable sort so same-step rows keep a deterministic order (pandas 2.x's
    # default quicksort is unstable and would shuffle ties run-to-run).
    df = df.sort_values("step", kind="stable").reset_index(drop=True)
    cut = int(len(df) * (1.0 - test_size))
    train_df, test_df = df.iloc[:cut].copy(), df.iloc[cut:].copy()
    n_pos_tr = int(train_df["isFraud"].sum())
    if max_train_rows and len(train_df) > max_train_rows:
        # Stratified cap: keep every fraud positive, downsample the negatives.
        pos = train_df[train_df["isFraud"] == 1]
        neg = train_df[train_df["isFraud"] == 0]
        n_neg_cap = max_train_rows - n_pos_tr
        sampled = n_neg_cap > 0 and n_neg_cap < len(neg)
        if sampled:
            neg = neg.sample(n=n_neg_cap, random_state=rs)
        train_df = pd.concat([pos, neg]).sort_values("step", kind="stable").reset_index(drop=True)
        if sampled:
            log.info(
                "Training window sampled: %d rows -> %d (stratified by fraud class, "
                "%d positives kept). Test window untouched (%d rows).",
                int(len(df) * (1.0 - test_size)),
                len(train_df),
                n_pos_tr,
                len(test_df),
            )
    y_tr_all = train_df["isFraud"].astype(int).to_numpy()
    y_te = test_df["isFraud"].astype(int).to_numpy()
    if y_tr_all.sum() == 0:
        log.warning("No positive (fraud) rows in the training window — benchmark is meaningless.")
    log.info(
        "Dataset: %s rows, %.2f%% fraud. Temporal split at row %d: %d train / %d test.",
        len(df),
        100 * float(y_all.mean()),
        cut,
        len(train_df),
        len(test_df),
    )

    # Backward-only features: no fitted statistics cross the split.
    X_tr_all = build_features(train_df)
    X_te = build_features(test_df)
    feature_names = list(X_tr_all.columns)
    y_tr = y_tr_all

    # --- Optuna HPO (Phase A.1): a separate, opt-in step — never part of a
    # retrain. Runs on the exact same backward-looking feature matrix and
    # TimeSeriesSplit folds as the benchmark below, so its CV number is
    # directly comparable to the defaults'. Writes the study db + importance
    # chart + hpo_best.json, then exits without touching the benchmark
    # artifacts.
    if args.hpo:
        hpo_cfg = bench_cfg.get("hpo", {})
        hpo_result = hpo.run_hpo(
            X_tr_all.to_numpy(),
            y_tr,
            outdir=outdir,
            random_state=rs,
            n_trials=args.n_trials or int(hpo_cfg.get("n_trials", 30)),
            n_splits=CV_FOLDS,
            n_estimators=int(hpo_cfg.get("n_estimators", 300)),
            min_improvement=float(hpo_cfg.get("min_improvement", 0.01)),
            promote=args.promote,
        )
        log.info(
            "HPO complete: %s",
            json.dumps({k: v for k, v in hpo_result.items() if k != "best_params"}, indent=2),
        )
        return

    # Adopted HPO params (model_bench/results/hpo_best.json with
    # `adopted: true`) override the LightGBM registry defaults — the study
    # provenance is recorded in the metadata below as `hpo_study_id`. A fresh
    # checkout / CI has no such file, so the weekly retrain always uses the
    # documented defaults.
    hpo_best: dict[str, Any] | None = None
    hpo_best_path = os.path.join(outdir, hpo.BEST_PARAMS_FILE)
    if os.path.exists(hpo_best_path):
        try:
            with open(hpo_best_path, encoding="utf-8") as fh:
                candidate = json.load(fh)
            # Defensive: only a well-formed *adopted* record changes what we
            # deploy; anything else (malformed, exploratory) falls back to the
            # documented registry defaults.
            if (
                candidate.get("adopted")
                and isinstance(candidate.get("best_params"), dict)
                and all(k in candidate for k in ("study_id", "study_name", "best_params"))
            ):
                hpo_best = candidate
                log.info(
                    "Using HPO-tuned LightGBM params (study id %s): %s",
                    hpo_best["study_id"],
                    hpo_best["best_params"],
                )
            else:
                log.info("Ignoring %s: not an adopted HPO record.", hpo_best_path)
        except (OSError, ValueError) as exc:  # corrupt/unreadable -> documented defaults
            log.warning("Ignoring unreadable %s: %s", hpo_best_path, exc)
            hpo_best = None
    lightgbm_params = (
        (hpo_best or {}).get("best_params") if (hpo_best or {}).get("adopted") else None
    )
    hpo_study_id, hpo_meta = hpo_provenance(hpo_best)

    registry = models.build_models(rs, lightgbm_params=lightgbm_params)

    # --- TimeSeriesSplit CV on the train window for model selection.
    tscv = TimeSeriesSplit(n_splits=CV_FOLDS)
    fold_metrics: dict[str, list[dict[str, float] | None]] = {name: [] for name in registry}
    holdout_scores: dict[str, np.ndarray] = {}
    holdout_metrics: dict[str, dict[str, float]] = {}
    timings: dict[str, float] = {}

    for tr_idx, va_idx in tscv.split(X_tr_all):
        fold_scaler = StandardScaler().fit(X_tr_all.to_numpy()[tr_idx])
        for name, model in registry.items():
            X_fit = (
                fold_scaler.transform(X_tr_all.to_numpy()[tr_idx])
                if name in models.NEEDS_SCALING
                else X_tr_all.to_numpy()[tr_idx]
            )
            X_val = (
                fold_scaler.transform(X_tr_all.to_numpy()[va_idx])
                if name in models.NEEDS_SCALING
                else X_tr_all.to_numpy()[va_idx]
            )
            m = clone(model)
            # The autoencoder learns to reconstruct normal behaviour → target is X.
            if isinstance(m, MLPRegressor):
                m.fit(X_fit, X_fit)
            else:
                m.fit(X_fit, y_tr[tr_idx])
            scores = models.predict_scores(m, X_val)
            fold_metrics[name].append(_fold_metrics(y_tr[va_idx], scores))

    cv_rows: list[dict[str, Any]] = []
    for name in registry:
        pr_mean, pr_std = _aggregate(fold_metrics[name], "pr_auc")
        roc_mean, roc_std = _aggregate(fold_metrics[name], "roc_auc")
        f1_mean, f1_std = _aggregate(fold_metrics[name], "f1")
        cv_rows.append(
            {
                "model": name,
                "pr_auc_mean": pr_mean,
                "pr_auc_std": pr_std,
                "roc_auc_mean": roc_mean,
                "roc_auc_std": roc_std,
                "f1_mean": f1_mean,
                "f1_std": f1_std,
            }
        )
        log.info(
            "%-38s CV pr_auc=%.3f ± %.3f  roc_auc=%.3f ± %.3f",
            name,
            pr_mean,
            pr_std,
            roc_mean,
            roc_std,
        )

    cv_table = pd.DataFrame(cv_rows)
    cv_table.to_csv(os.path.join(outdir, "metrics_table.csv"), index=False)
    evaluate.plot_bar_comparison(cv_table, outdir)

    # --- Holdout (temporal test window) scores for curves + final numbers.
    full_scaler = StandardScaler().fit(X_tr_all.to_numpy())
    for name, model in registry.items():
        t0 = time.perf_counter()
        m = clone(model)
        X_fit = (
            full_scaler.transform(X_tr_all.to_numpy())
            if name in models.NEEDS_SCALING
            else X_tr_all.to_numpy()
        )
        X_eval = (
            full_scaler.transform(X_te.to_numpy())
            if name in models.NEEDS_SCALING
            else X_te.to_numpy()
        )
        if isinstance(m, MLPRegressor):
            m.fit(X_fit, X_fit)
        else:
            m.fit(X_fit, y_tr)
        holdout_scores[name] = models.predict_scores(m, X_eval)
        holdout_metrics[name] = evaluate.compute_metrics(y_te, holdout_scores[name])
        timings[name] = round(time.perf_counter() - t0, 2)

    evaluate.plot_curves(y_te, holdout_scores, outdir)
    evaluate.plot_confusion_grid(y_te, holdout_scores, outdir)

    # --- Winner: best mean CV PR-AUC, with a transparent explainability tie-break.
    # The app ships per-transaction SHAP explanations (fraud page) that only work
    # for the LightGBM path (native `pred_contrib`). So when the CV leader and a
    # SHAP-capable model are statistically indistinguishable (within
    # `model_bench.shap_tolerance_std` standard deviations of the leader's own
    # CV std), we prefer the explainable model. This is a deliberate, documented
    # product trade-off — transparent numbers over a marginal AUC gain — and the
    # policy is recorded in the metadata so the choice is auditable.
    SHAP_CAPABLE = ("Gradient Boosting (LightGBM)",)
    shap_preference = bool(bench_cfg.get("shap_preference", True))
    shap_tolerance_std = float(bench_cfg.get("shap_tolerance_std", 1.0))

    def cv_or_holdout(name: str) -> float:
        row = cv_table.loc[cv_table["model"] == name, "pr_auc_mean"].iloc[0]
        if pd.isna(row):
            return float(holdout_metrics[name]["pr_auc"])
        return float(row)

    leader = max(registry, key=cv_or_holdout)
    leader_std = float(cv_table.loc[cv_table["model"] == leader, "pr_auc_std"].iloc[0])
    tolerance = shap_tolerance_std * (leader_std if pd.notna(leader_std) else 0.0)
    within = [n for n in registry if (cv_or_holdout(leader) - cv_or_holdout(n)) <= tolerance]
    if shap_preference:
        explainable = [n for n in within if n in SHAP_CAPABLE]
        best_name = max(explainable, key=cv_or_holdout) if explainable else leader
    else:
        best_name = leader
    if best_name != leader:
        log.info(
            "CV leader %s (%.3f) is within %.3f of the SHAP-capable %s — preferring it "
            "for per-transaction explanations (see metadata selection policy).",
            leader,
            cv_or_holdout(leader),
            tolerance,
            best_name,
        )
    best_cv = cv_table.loc[cv_table["model"] == best_name].iloc[0]
    best_holdout = holdout_metrics[best_name]
    log.info(
        "Best model by mean %s (CV): %s (pr_auc=%.3f ± %.3f; holdout %.3f)",
        selection_metric,
        best_name,
        float(best_cv["pr_auc_mean"]),
        float(best_cv["pr_auc_std"]),
        float(best_holdout["pr_auc"]),
    )

    # --- Refit the winner on ALL train data for deployment.
    best_model = clone(registry[best_name])
    X_all = X_tr_all.to_numpy()
    X_fit_all = full_scaler.transform(X_all) if best_name in models.NEEDS_SCALING else X_all
    if isinstance(best_model, MLPRegressor):
        best_model.fit(X_fit_all, X_fit_all)
    else:
        best_model.fit(X_fit_all, y_tr)
    joblib.dump(best_model, best_path)
    write_signature(best_path)  # C.2.4 — sign the standalone model artifact too

    iforest = clone(registry["Isolation Forest"])
    iforest.fit(full_scaler.transform(X_all))
    joblib.dump(
        {
            "best_model": best_model,
            "best_model_name": best_name,
            "isolation_forest": iforest,
            "scaler": full_scaler,
            "feature_names": feature_names,
            "needs_scaling": best_name in models.NEEDS_SCALING,
            "metrics": {k: float(v) for k, v in best_holdout.items()},
        },
        bundle_path,
    )
    # C.2.4: sign the bundle so tools.py can refuse a tampered pickle before
    # joblib.load. Key = FINSIGHT_BUNDLE_KEY env var (demo default otherwise).
    bundle_sig = write_signature(bundle_path)
    evaluate.plot_feature_importance(best_model, X_te.to_numpy(), y_te, feature_names, outdir)

    # --- data-gen v2 diagnostics: per-archetype recall, cohort fairness,
    # --- temporal stability, and calibration, all on the holdout window.
    win_scores = holdout_scores[best_name]
    archetype_recall = pd.DataFrame()
    cohort_fairness = pd.DataFrame()
    temporal = pd.DataFrame()
    calib = pd.DataFrame()
    if "fraud_archetype" in test_df.columns and test_df["fraud_archetype"].notna().any():
        archetype_recall = evaluate.per_archetype_recall(
            y_te, win_scores, test_df["fraud_archetype"].to_numpy()
        )
        archetype_recall.to_csv(os.path.join(outdir, "per_archetype_recall.csv"), index=False)
        if not archetype_recall.empty:
            evaluate.plot_archetype_recall(archetype_recall, outdir)
        log.info(
            "Per-archetype recall (holdout): %s",
            archetype_recall.to_string(index=False).replace("\n", " | "),
        )
    if "persona_archetype" in test_df.columns and test_df["persona_archetype"].notna().any():
        cohort_fairness = evaluate.cohort_recall(
            y_te, win_scores, test_df["persona_archetype"].to_numpy()
        )
        cohort_fairness.to_csv(os.path.join(outdir, "cohort_fairness.csv"), index=False)
        log.info(
            "Cohort fairness (holdout): %s",
            cohort_fairness.to_string(index=False).replace("\n", " | "),
        )
    if "step" in test_df.columns and len(test_df) > 1:
        temporal = evaluate.temporal_stability(y_te, win_scores, test_df["step"].to_numpy())
        temporal.to_csv(os.path.join(outdir, "temporal_stability.csv"), index=False)
        if not temporal.empty:
            evaluate.plot_temporal_stability(temporal, outdir)
        log.info(
            "Temporal stability (holdout): %s", temporal.to_string(index=False).replace("\n", " | ")
        )
    calib = evaluate.calibration_curve(y_te, win_scores)
    calib.to_csv(os.path.join(outdir, "calibration_curve.csv"), index=False)
    if not calib.empty:
        evaluate.plot_calibration_curve(calib, outdir)
    brier = evaluate.brier_score(y_te, win_scores)
    ece = evaluate.expected_calibration_error(y_te, win_scores)
    log.info("Calibration (holdout): brier=%.4f ece=%.4f", brier, ece)

    pr_mean, pr_std = _aggregate(fold_metrics[best_name], "pr_auc")
    roc_mean, roc_std = _aggregate(fold_metrics[best_name], "roc_auc")
    f1_mean, f1_std = _aggregate(fold_metrics[best_name], "f1")
    metadata = {
        "algorithm": best_name,
        "selection_metric": f"{selection_metric} (mean over TimeSeriesSplit CV)",
        "metrics_on_holdout": {k: float(v) for k, v in best_holdout.items()},
        "cv_folds": CV_FOLDS,
        "pr_auc_mean": pr_mean,
        "pr_auc_std": pr_std,
        "roc_auc_mean": roc_mean,
        "roc_auc_std": roc_std,
        "f1_mean": f1_mean,
        "f1_std": f1_std,
        "per_model_cv": [
            {
                "model": r["model"],
                "pr_auc_mean": None if pd.isna(r["pr_auc_mean"]) else float(r["pr_auc_mean"]),
                "pr_auc_std": None if pd.isna(r["pr_auc_std"]) else float(r["pr_auc_std"]),
                "roc_auc_mean": None if pd.isna(r["roc_auc_mean"]) else float(r["roc_auc_mean"]),
                "f1_mean": None if pd.isna(r["f1_mean"]) else float(r["f1_mean"]),
            }
            for r in cv_rows
        ],
        "training_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_sha(),
        "feature_list": feature_names,
        "dataset": {
            "rows": len(df),
            "fraud_rate": round(float(y_all.mean()), 4),
            "path": args.data,
            "train_rows": len(train_df),
            "test_rows": len(test_df),
            "personas": int(df["persona_id"].nunique()) if "persona_id" in df.columns else None,
            "focal_personas": (
                int(df.loc[df["is_focal_user"], "persona_id"].nunique())
                if "persona_id" in df.columns
                else None
            ),
        },
        "archetype_recall": (
            [
                {
                    "archetype": str(r["archetype"]),
                    "recall": None if pd.isna(r["recall"]) else float(r["recall"]),
                    "support": int(r["support"]),
                }
                for r in archetype_recall.to_dict(orient="records")
            ]
            if not archetype_recall.empty
            else []
        ),
        "cohort_fairness": (
            [
                {
                    "cohort": str(r["cohort"]),
                    "recall": None if pd.isna(r["recall"]) else float(r["recall"]),
                    "support": int(r["support"]),
                    "disparity_ratio": (
                        None if pd.isna(r.get("disparity_ratio")) else float(r["disparity_ratio"])
                    ),
                }
                for r in cohort_fairness.to_dict(orient="records")
            ]
            if not cohort_fairness.empty
            else []
        ),
        "temporal_stability": (
            [
                {
                    "bucket": int(r["bucket"]),
                    "start_step": int(r["start_step"]),
                    "end_step": int(r["end_step"]),
                    "precision": None if pd.isna(r["precision"]) else float(r["precision"]),
                    "recall": None if pd.isna(r["recall"]) else float(r["recall"]),
                    "support": int(r["support"]),
                }
                for r in temporal.to_dict(orient="records")
            ]
            if not temporal.empty
            else []
        ),
        "calibration": {
            "brier": brier,
            "expected_calibration_error": ece,
            "curve_rows": (
                [
                    {
                        "bin": int(r["bin"]),
                        "mean_pred": None if pd.isna(r["mean_pred"]) else float(r["mean_pred"]),
                        "frac_pos": None if pd.isna(r["frac_pos"]) else float(r["frac_pos"]),
                        "count": int(r["count"]),
                    }
                    for r in calib.to_dict(orient="records")
                ]
                if not calib.empty
                else []
            ),
        },
        "hpo_study_id": hpo_study_id,
        "hpo": hpo_meta,
        "bundle_signature": {
            "algorithm": ALGORITHM,
            "key_origin": key_origin(),
            "digest_prefix": bundle_sig[:16],
        },
        "config": {
            "random_state": rs,
            "test_size": test_size,
            "split": "temporal (sort by step, no shuffle)",
            "cv": f"TimeSeriesSplit(k={CV_FOLDS})",
            "features": "strictly backward-looking",
            "max_train_rows": max_train_rows or "full",
            "selection_policy": (
                "best mean CV PR-AUC, preferring the SHAP-capable model "
                f"({SHAP_CAPABLE[0]}) when within {shap_tolerance_std:g} CV std of "
                "the leader (explainability tie-break, see KNOWN_LIMITATIONS)"
                if shap_preference
                else "best mean CV PR-AUC only (no explainability preference)"
            ),
        },
    }
    with open(metadata_path, "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2)
    # Phase A.4: MODEL_CARD.md is generated from the metadata above so the card
    # can never drift from the benchmark. Regenerated on every train.
    model_card_path = os.path.join(os.path.dirname(metadata_path) or ".", "MODEL_CARD.md")
    with open(model_card_path, "w", encoding="utf-8") as fh:
        fh.write(evaluate.model_card_markdown(metadata))
    log.info(
        "Artifacts written: %s, %s, %s, %s", best_path, bundle_path, metadata_path, model_card_path
    )


if __name__ == "__main__":
    main()
