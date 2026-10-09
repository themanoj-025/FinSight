"""Optuna HPO smoke tests (Phase A.1 acceptance).

The acceptance criterion is deliberately modest for CI: the HPO objective
returns a finite, in-range score for a small trial budget, a tiny study runs
end-to-end, and ``run_hpo`` writes the provenance artifacts and respects the
adoption gate — no full study, no bench-tier data, no wall-clock surprises.
"""

import json

import numpy as np
import pytest

from finance_agent.features import build_features
from generate_data import generate
from model_bench import hpo, models

pytestmark = pytest.mark.filterwarnings("ignore::optuna.exceptions.ExperimentalWarning")

optuna = pytest.importorskip("optuna")

# optuna's matplotlib `plot_param_importances` is experimental and emits an
# ExperimentalWarning on every chart render — the chart is a real artifact,
# but the warning is noise in CI output.


def _tiny_xy() -> tuple[np.ndarray, np.ndarray]:
    """A small, leakage-safe feature matrix + labels.

    Mirrors train_and_compare.py: sort by step, then build the strictly
    backward-looking features on the sorted frame so rows align with ``y``.
    """
    df = (
        generate(
            days=30,
            seed=7,
            user="U_Alex",
            n_background_accounts=20,
            n_fraud_pairs=2,
            start_date="2025-01-01",
        )
        .sort_values("step")
        .reset_index(drop=True)
    )
    X = build_features(df).to_numpy(dtype=float)
    y = df["isFraud"].astype(int).to_numpy()
    assert y.sum() > 0, "fixture must contain fraud positives"
    return X, y


def test_objective_returns_finite_score_for_small_budget() -> None:
    X, y = _tiny_xy()
    # Values must lie inside each suggest_* distribution — notably reg_alpha /
    # reg_lambda are log-uniform over [1e-8, 10], so 0.0 would be invalid.
    trial = optuna.trial.FixedTrial(
        {
            "num_leaves": 64,
            "learning_rate": 0.1,
            "min_child_samples": 20,
            "reg_alpha": 1e-4,
            "reg_lambda": 1e-4,
            "feature_fraction": 0.8,
        }
    )
    score = hpo.objective(trial, X, y, random_state=7, n_splits=2, n_estimators=20)
    assert np.isfinite(score)
    assert 0.0 <= score <= 1.0


def test_tiny_study_smoke_and_registry_accepts_tuned_params() -> None:
    X, y = _tiny_xy()
    study = optuna.create_study(direction="maximize")
    study.optimize(
        lambda t: hpo.objective(t, X, y, random_state=7, n_splits=2, n_estimators=20),
        n_trials=3,
    )
    assert np.isfinite(study.best_value)
    assert 0.0 <= study.best_value <= 1.0
    # the adopted params must flow through the registry (make train path)
    tuned = {k: study.best_params.get(k, hpo.DEFAULT_LGBM_PARAMS[k]) for k in hpo.TUNED_PARAMS}
    registry = models.build_models(42, lightgbm_params=tuned)
    assert models.GB_LABEL in registry
    assert registry[models.GB_LABEL].learning_rate == tuned["learning_rate"]


def test_run_hpo_writes_provenance_and_respects_adoption_gate(tmp_path) -> None:
    X, y = _tiny_xy()
    out = tmp_path / "hpo"
    out.mkdir()

    # Exploratory run: no --promote means nothing is adopted AND no adoption
    # record is written — an exploratory run must never clobber an existing
    # adoption (which the next `make train` would otherwise silently lose).
    result = hpo.run_hpo(
        X,
        y,
        outdir=str(out),
        random_state=7,
        n_trials=2,
        n_splits=2,
        n_estimators=20,
        min_improvement=-10.0,
        promote=False,
    )
    assert result["adopted"] is False
    assert isinstance(result["study_id"], int)
    assert set(hpo.TUNED_PARAMS) <= set(result["best_params"])
    assert not (out / hpo.BEST_PARAMS_FILE).exists(), "exploratory run must not write a record"

    # Promotion run against the same study (load_if_exists resumes it): the
    # gate passes (trivially here), so the params are adopted for the next
    # `make train` and the decision record round-trips the returned dict.
    result2 = hpo.run_hpo(
        X,
        y,
        outdir=str(out),
        random_state=7,
        n_trials=2,
        n_splits=2,
        n_estimators=20,
        min_improvement=-10.0,
        promote=True,
    )
    assert result2["adopted"] is True
    assert result2["study_id"] == result["study_id"], "promotion must resume the same study"
    best_json = json.loads((out / hpo.BEST_PARAMS_FILE).read_text(encoding="utf-8"))
    assert best_json == result2
    # The importance chart is guarded on a tiny study: either written or
    # absent — never an exception.
    chart = out / hpo.IMPORTANCE_CHART
    assert not chart.exists() or chart.stat().st_size > 0


def test_hpo_provenance_metadata_helper() -> None:
    """The metadata provenance (Phase A.1 acceptance) is explicit and correct."""
    from model_bench.train_and_compare import hpo_provenance

    assert hpo_provenance(None) == (None, None)
    assert hpo_provenance({"adopted": False, "best_params": {}}) == (None, None)
    adopted = {
        "adopted": True,
        "study_id": 7,
        "study_name": "lgbm-hpo-rs42",
        "n_trials": 5,
        "baseline_value": 0.4,
        "best_value": 0.5,
        "improvement": 0.1,
        "min_improvement": 0.01,
        "best_params": {"num_leaves": 64, "learning_rate": 0.1},
    }
    study_id, block = hpo_provenance(adopted)
    assert study_id == 7
    assert block == {
        "study_id": 7,
        "study_name": "lgbm-hpo-rs42",
        "n_trials": 5,
        "baseline_value": 0.4,
        "best_value": 0.5,
        "improvement": 0.1,
        "min_improvement": 0.01,
        "best_params": {"num_leaves": 64, "learning_rate": 0.1},
    }
