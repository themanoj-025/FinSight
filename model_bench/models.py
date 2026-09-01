"""Model registry — one factory per algorithm family.

The registry deliberately covers four *classes* of approach so the benchmark
tells a story in interviews:
  1. linear / interpretable baseline        (Logistic Regression)
  2. tree ensembles                         (Random Forest, Gradient Boosting)
  3. large-margin / high-dimensional        (SGD as a scaled linear SVM)
  4. unsupervised anomaly detection         (Isolation Forest)
     plus a neural reconstruction baseline (MLP autoencoder, optional)
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.linear_model import LogisticRegression, SGDClassifier
from sklearn.neural_network import MLPRegressor

try:  # LightGBM is preferred when present; xgboost and sklearn are fallbacks.
    from lightgbm import LGBMClassifier

    def _gradient_boosting(rs: int, params: dict | None = None) -> BaseEstimator:
        """LightGBM with optional HPO-tuned overrides (model_bench/hpo.py).

        ``params`` wins over the registry defaults for the tuned keys only;
        the safety/identity settings (class_weight, random_state, verbose,
        n_jobs) can never be overridden. Merged through one kwargs dict
        because ``f(x=..., **{"x": ...})`` is a Python error — duplicates
        must be resolved before the call.
        """
        kwargs: dict[str, Any] = {
            "n_estimators": 300,
            "learning_rate": 0.05,
            "class_weight": "balanced",
            "random_state": rs,
            "verbose": -1,
            "n_jobs": -1,
        }
        if params:
            kwargs.update(params)
        return LGBMClassifier(**kwargs)

    GB_LABEL = "Gradient Boosting (LightGBM)"
except ImportError:  # pragma: no cover
    from sklearn.ensemble import GradientBoostingClassifier

    def _gradient_boosting(rs: int, params: dict | None = None) -> BaseEstimator:
        # The sklearn fallback has no equivalent of the tuned LightGBM params;
        # accepted for signature parity and ignored.
        return GradientBoostingClassifier(n_estimators=300, learning_rate=0.05, random_state=rs)

    GB_LABEL = "Gradient Boosting (sklearn)"


def build_models(
    random_state: int = 42, lightgbm_params: dict | None = None
) -> dict[str, BaseEstimator]:
    """Return `{model_label: untrained_estimator}` for the benchmark.

    ``lightgbm_params`` — the adopted best params from an HPO study
    (model_bench/hpo.py) — override the LightGBM defaults when provided;
    None keeps the registry defaults.
    """
    return {
        "Logistic Regression": LogisticRegression(
            max_iter=2000, class_weight="balanced", random_state=random_state
        ),
        "Random Forest": RandomForestClassifier(
            n_estimators=250,
            class_weight="balanced",
            random_state=random_state,
            n_jobs=-1,
        ),
        GB_LABEL: _gradient_boosting(random_state, lightgbm_params),
        "SGD (linear SVM)": SGDClassifier(
            loss="modified_huber",
            class_weight="balanced",
            random_state=random_state,
            max_iter=2000,
        ),
        "Isolation Forest": IsolationForest(
            n_estimators=250, random_state=random_state, contamination="auto"
        ),
        "MLP Autoencoder (recon. error)": MLPRegressor(
            hidden_layer_sizes=(32, 16),
            max_iter=400,
            random_state=random_state,
            early_stopping=True,
        ),
    }


# Models trained on scaled features (linear models and neural nets).
NEEDS_SCALING = {"Logistic Regression", "SGD (linear SVM)", "MLP Autoencoder (recon. error)"}


def predict_scores(model: BaseEstimator, X: np.ndarray) -> np.ndarray:
    """Return a score where *higher = more fraud-like* for any model in the registry.

    - classifiers with predict_proba -> positive-class probability
    - IsolationForest -> negative of the sample score
    - the autoencoder (MLPRegressor) -> reconstruction error (MSE per row)
    - anything else -> decision function
    """
    # Some estimators (e.g. LightGBM) record feature names at fit time even
    # when given a plain array; hand the names back to avoid spurious warnings.
    names = getattr(model, "feature_names_in_", None)
    if names is not None and X.ndim == 2:
        X = pd.DataFrame(X, columns=list(names))
    if hasattr(model, "predict_proba"):
        return model.predict_proba(X)[:, 1].astype(float)
    if isinstance(model, IsolationForest):
        return -np.asarray(model.score_samples(X), dtype=float)
    if isinstance(model, MLPRegressor):
        pred = model.predict(X)
        return np.mean((np.asarray(X, dtype=float) - pred) ** 2, axis=1)
    if hasattr(model, "decision_function"):
        return np.asarray(model.decision_function(X), dtype=float)
    return np.asarray(model.predict(X), dtype=float)
