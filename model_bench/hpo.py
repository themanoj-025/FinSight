"""Optuna hyperparameter optimization (Phase A.1).

``make hpo`` (``train_and_compare.py --hpo``) runs an Optuna study over the
LightGBM family's key hyperparameters (``num_leaves``, ``learning_rate``,
``min_child_samples``, ``reg_alpha``, ``reg_lambda``, ``feature_fraction``)
using the **same** mean-PR-AUC-over-``TimeSeriesSplit`` objective the
benchmark uses for model selection, so a tuned model's CV number is directly
comparable to the default model's.

Design rules (mirroring the rest of the pipeline):

* **Leakage-safe by construction** — the study runs on the exact
  backward-looking feature matrix ``X_tr_all`` the benchmark computes once on
  the temporal train window; folds split that matrix the same way
  ``train_and_compare.py`` does. No feature recomputation per trial, no
  new leakage surface.
* **Opt-in and separated** — HPO is a manual/monthly step, never part of the
  weekly retrain. Optuna is an optional extra (``pip install -e ".[hpo]"``),
  so every ``optuna`` import here is lazy: ``make train`` and the app never
  need it.
* **Noise-guarded adoption** — the tuned params become the new defaults only
  after a documented review step: a human must run with ``--promote`` AND the
  best trial must beat the current registry defaults by at least
  ``model_bench.hpo.min_improvement`` (default 0.01 PR-AUC). The decision
  lands in ``model_bench/results/hpo_best.json`` and, once adopted, the
  study provenance is recorded in ``best_model_metadata.json`` as
  ``hpo_study_id``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, cast

import numpy as np
from sklearn.model_selection import TimeSeriesSplit

from model_bench import evaluate, models

log = logging.getLogger("model_bench.hpo")

# The six tuned hyperparameters — exactly the spec'd list for the LightGBM
# family. Everything else (n_estimators, class_weight, random_state, n_jobs,
# verbose) stays at the registry defaults so trials are comparable to the
# deployed model.
TUNED_PARAMS = (
    "num_leaves",
    "learning_rate",
    "min_child_samples",
    "reg_alpha",
    "reg_lambda",
    "feature_fraction",
)

# What the registry effectively uses today (LGBM defaults except the
# registry's learning_rate=0.05 / n_estimators=300). The baseline is
# evaluated with these so `improvement` measures the gain over the *deployed*
# configuration, not over arbitrary defaults.
DEFAULT_LGBM_PARAMS: dict[str, Any] = {
    "num_leaves": 31,
    "learning_rate": 0.05,
    "min_child_samples": 20,
    "reg_alpha": 0.0,
    "reg_lambda": 0.0,
    "feature_fraction": 1.0,
}

IMPORTANCE_CHART = "hpo_param_importance.png"
BEST_PARAMS_FILE = "hpo_best.json"
STORAGE_FILE = "hpo_study.db"


def suggest_lgbm_params(trial: Any) -> dict[str, Any]:
    """One hyperparameter combination from the search space for ``trial``."""
    return {
        "num_leaves": trial.suggest_int("num_leaves", 16, 256, log=True),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
        "min_child_samples": trial.suggest_int("min_child_samples", 5, 100),
        "reg_alpha": trial.suggest_float("reg_alpha", 1e-8, 10.0, log=True),
        "reg_lambda": trial.suggest_float("reg_lambda", 1e-8, 10.0, log=True),
        "feature_fraction": trial.suggest_float("feature_fraction", 0.5, 1.0),
    }


def _fold_pr_auc(y_true: np.ndarray, scores: np.ndarray) -> float | None:
    """PR-AUC for one fold, or None when the fold has a single class."""
    if len(np.unique(y_true)) < 2:
        return None
    return float(evaluate.compute_metrics(y_true, scores)["pr_auc"])


def _cv_pr_auc(
    X: np.ndarray,
    y: np.ndarray,
    params: dict[str, Any],
    *,
    random_state: int = 42,
    n_splits: int = 5,
    n_estimators: int = 300,
) -> float:
    """Mean PR-AUC over time-series CV for an explicit LightGBM param dict.

    The single source of truth for both the HPO objective and the baseline:
    same folds and score path as the benchmark's model selection, so the two
    numbers are directly comparable. Returns ``-1.0`` when no fold has both
    classes (degenerate data) so callers never optimize a NaN.
    """
    from lightgbm import LGBMClassifier

    tscv = TimeSeriesSplit(n_splits=n_splits)
    pr_aucs: list[float] = []
    for tr_idx, va_idx in tscv.split(X):
        model = LGBMClassifier(
            n_estimators=n_estimators,
            class_weight="balanced",
            random_state=random_state,
            verbose=-1,
            n_jobs=-1,
            **params,
        )
        model.fit(X[tr_idx], y[tr_idx])
        val = _fold_pr_auc(y[va_idx], models.predict_scores(model, X[va_idx]))
        if val is not None:
            pr_aucs.append(val)
    if not pr_aucs:
        return -1.0
    return float(np.mean(pr_aucs))


def objective(
    trial: Any,
    X: np.ndarray,
    y: np.ndarray,
    *,
    random_state: int = 42,
    n_splits: int = 5,
    n_estimators: int = 300,
) -> float:
    """Mean PR-AUC over time-series CV for one hyperparameter combination.

    Mirrors the benchmark's model-selection loop for the LightGBM family
    (same folds, same score path), so the number is comparable to the CV
    figure in ``best_model_metadata.json``.
    """
    return _cv_pr_auc(
        X,
        y,
        suggest_lgbm_params(trial),
        random_state=random_state,
        n_splits=n_splits,
        n_estimators=n_estimators,
    )


def evaluate_params(
    X: np.ndarray,
    y: np.ndarray,
    params: dict[str, Any],
    *,
    random_state: int = 42,
    n_splits: int = 5,
    n_estimators: int = 300,
) -> float:
    """Mean CV PR-AUC for an explicit param dict — deterministic, no Optuna.

    Used for the registry-defaults baseline so ``improvement`` is measured
    against what is actually deployed.
    """
    return _cv_pr_auc(
        X,
        y,
        params,
        random_state=random_state,
        n_splits=n_splits,
        n_estimators=n_estimators,
    )


def _storage_url(path: str) -> str:
    """SQLite URL that works on every platform (forward slashes in the path)."""
    return "sqlite:///" + Path(path).resolve().as_posix()


def _write_importance_chart(study: Any, outdir: str) -> str | None:
    """Render the Optuna parameter-importance chart, guarded.

    fANOVA needs more than one completed trial and non-constant suggested
    params; a tiny/early study legitimately cannot produce the chart, so a
    failure logs a warning and yields no chart rather than crashing the step.
    """
    try:
        from matplotlib.figure import Figure
        from optuna.visualization.matplotlib import plot_param_importances

        ax = plot_param_importances(study)  # optuna's matplotlib plots return an Axes
        # The Axes is always attached to a real Figure at runtime; cast past
        # matplotlib's Figure | SubFigure union for the save/close calls.
        fig = cast(Figure, ax.figure)
        path = os.path.join(outdir, IMPORTANCE_CHART)
        fig.savefig(path, dpi=110, bbox_inches="tight", facecolor="#0D1526")
        import matplotlib.pyplot as plt

        plt.close(fig)
        log.info("Parameter-importance chart written to %s", path)
        return path
    except (RuntimeError, ValueError) as exc:
        log.warning("Parameter-importance chart not produced: %s", exc)
        return None


def run_hpo(
    X: np.ndarray,
    y: np.ndarray,
    *,
    outdir: str,
    random_state: int = 42,
    n_trials: int = 30,
    n_splits: int = 5,
    n_estimators: int = 300,
    min_improvement: float = 0.01,
    promote: bool = False,
) -> dict[str, Any]:
    """Run (or continue) the Optuna study and write the provenance artifacts.

    Returns a JSON-serializable provenance dict and writes:

    * ``<outdir>/hpo_study.db`` — optuna-dashboard-compatible SQLite storage,
      so a later run with the same study name resumes the same study
      (monthly cadence accumulates trials).
    * ``<outdir>/hpo_param_importance.png`` — parameter-importance chart
      (guarded; appears once the study has enough trials).
    * ``<outdir>/hpo_best.json`` — the adoption decision + tuned params;
      ``make train`` reads this and uses the tuned params (and records
      ``hpo_study_id`` in the metadata) only when ``adopted`` is true.
    """
    import optuna  # optional extra — installed via `pip install -e ".[hpo]"`

    os.makedirs(outdir, exist_ok=True)
    storage_path = os.path.join(outdir, STORAGE_FILE)
    best_params_path = os.path.join(outdir, BEST_PARAMS_FILE)
    study_name = f"lgbm-hpo-rs{random_state}"

    storage = optuna.storages.RDBStorage(url=_storage_url(storage_path))
    study = optuna.create_study(
        study_name=study_name,
        storage=storage,
        load_if_exists=True,
        direction="maximize",
    )
    # optuna 4 removed the public `Study.study_id`; `_study_id` is the
    # accessor it keeps for the id we record for provenance.
    study_id = int(study._study_id)
    log.info(
        "HPO study %s (id=%s): %d trial(s) over TimeSeriesSplit(%d) on %d rows",
        study_name,
        study_id,
        n_trials,
        n_splits,
        len(X),
    )
    study.optimize(
        lambda t: objective(
            t,
            X,
            y,
            random_state=random_state,
            n_splits=n_splits,
            n_estimators=n_estimators,
        ),
        n_trials=n_trials,
    )

    baseline_value = evaluate_params(
        X,
        y,
        DEFAULT_LGBM_PARAMS,
        random_state=random_state,
        n_splits=n_splits,
        n_estimators=n_estimators,
    )
    best = study.best_trial
    best_val = best.value
    best_value = float(best_val) if best_val is not None else -1.0
    improvement = best_value - baseline_value
    # Adoption = a human signed off (--promote) AND a real score (not the -1.0
    # degenerate sentinel) AND the gain clears the CV-noise guard. Without
    # --promote the run is exploratory and never changes what `make train`
    # deploys.
    adopted = bool(promote and best_value > 0.0 and improvement >= min_improvement)

    result: dict[str, Any] = {
        "study_id": study_id,
        "study_name": study_name,
        # Total completed trials in the study — a resumed run (load_if_exists)
        # accumulates trials across invocations, so the recorded number must
        # reflect the whole study, not just this run's budget.
        "n_trials": len(study.trials),
        "baseline_value": round(baseline_value, 4),
        "best_value": round(best_value, 4),
        "improvement": round(improvement, 4),
        "min_improvement": min_improvement,
        "best_params": {k: best.params[k] for k in TUNED_PARAMS},
        "promoted": promote,
        "adopted": adopted,
        "storage_path": storage_path,
    }
    # The adoption decision is only ever written when a human runs --promote:
    # an exploratory `make hpo` must never clobber an existing adoption record
    # (which the next `make train` would otherwise silently lose).
    if promote:
        with open(best_params_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    else:
        log.info("Exploratory run — %s left untouched.", best_params_path)

    # The printed comparison IS the documented review step.
    log.info("Baseline (registry defaults): CV PR-AUC %.4f", baseline_value)
    log.info(
        "Best trial %s: CV PR-AUC %.4f  (improvement %+.4f vs baseline)",
        best.number,
        best_value,
        improvement,
    )
    log.info(
        "Adoption gate: improvement %.4f >= %.4f AND --promote=%s  ->  adopted=%s",
        improvement,
        min_improvement,
        promote,
        adopted,
    )
    log.info("Tuned params: %s", {k: result["best_params"][k] for k in TUNED_PARAMS})
    if adopted:
        log.info(
            "Tuned params adopted — the next `make train` uses them and records "
            "hpo_study_id=%s in best_model_metadata.json.",
            result["study_id"],
        )
    elif promote:
        log.warning(
            "Improvement %.4f does not clear the %.4f gate — not adopting "
            "(avoid chasing CV noise).",
            improvement,
            min_improvement,
        )
    else:
        log.info(
            "Exploratory run (no --promote) — nothing adopted. Promote with: "
            "make hpo-promote  (or review the study with: optuna-dashboard %s)",
            storage_path,
        )
    _write_importance_chart(study, outdir)
    return result
