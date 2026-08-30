"""Model registry tests (Phase 3.2): predict_scores branches + scaling paths."""

import numpy as np
import pytest
from sklearn.ensemble import IsolationForest
from sklearn.linear_model import LogisticRegression
from sklearn.neural_network import MLPRegressor
from sklearn.preprocessing import StandardScaler

from model_bench import models


@pytest.fixture()
def xy() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    X = rng.normal(size=(200, 6))
    y = (X[:, 0] + X[:, 1] > 1.0).astype(int)
    return X, y


def test_predict_scores_probability_classifier(xy) -> None:
    X, y = xy
    clf = LogisticRegression(max_iter=1000, random_state=0).fit(X, y)
    scores = models.predict_scores(clf, X)
    assert scores.shape == (200,)
    assert np.isfinite(scores).all()
    assert scores.min() >= 0.0 and scores.max() <= 1.0


def test_predict_scores_isolation_forest(xy) -> None:
    X, _ = xy
    iso = IsolationForest(random_state=0, contamination="auto").fit(X)
    scores = models.predict_scores(iso, X)
    assert scores.shape == (200,)
    assert np.isfinite(scores).all()
    # higher score = more anomalous (negative of the raw sample score)


def test_predict_scores_autoencoder_reconstruction_error(xy) -> None:
    X, _ = xy
    mlp = MLPRegressor(
        hidden_layer_sizes=(8, 4), max_iter=300, random_state=0, early_stopping=True
    ).fit(X, X)
    scores = models.predict_scores(mlp, X)
    assert scores.shape == (200,)
    assert (scores >= 0.0).all()
    assert np.isfinite(scores).all()


def test_predict_scores_decision_function_fallback(xy) -> None:
    """SGDClassifier has decision_function and no predict_proba on some losses."""
    X, y = xy
    from sklearn.linear_model import SGDClassifier

    sgd = SGDClassifier(loss="hinge", random_state=0, max_iter=1000).fit(X, y)
    scores = models.predict_scores(sgd, X)
    assert scores.shape == (200,)
    assert np.isfinite(scores).all()


def test_predict_scores_with_scaling_required_path(xy) -> None:
    """Linear/neural models are trained on scaled features; inference must too."""
    X, y = xy
    scaler = StandardScaler().fit(X)
    Xs = scaler.transform(X)
    clf = LogisticRegression(max_iter=1000, random_state=0).fit(Xs, y)
    scores = models.predict_scores(clf, scaler.transform(X))
    assert scores.shape == (200,)


def test_predict_scores_handles_feature_names_from_lightgbm_style_fit(xy) -> None:
    """Estimators that record feature_names_in_ must still accept plain arrays."""
    X, y = xy
    try:
        from lightgbm import LGBMClassifier

        gbm = LGBMClassifier(n_estimators=10, verbose=-1, random_state=0)
    except ImportError:  # pragma: no cover
        pytest.skip("lightgbm not installed")
    import pandas as pd

    gbm.fit(pd.DataFrame(X, columns=[f"f{i}" for i in range(X.shape[1])]), y)
    scores = models.predict_scores(gbm, X)
    assert scores.shape == (200,)


def test_registry_has_six_models() -> None:
    reg = models.build_models(42)
    assert len(reg) == 6
    assert "Isolation Forest" in reg
    assert "MLP Autoencoder (recon. error)" in reg
