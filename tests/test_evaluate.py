"""Evaluation tests (Phase 3.2): compute_metrics sanity + permutation path."""

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from model_bench import evaluate


def test_compute_metrics_sane_on_small_labeled_set():
    y = np.array([0, 0, 0, 0, 1, 1, 1, 1])
    s = np.array([0.05, 0.1, 0.2, 0.3, 0.55, 0.7, 0.85, 0.95])
    m = evaluate.compute_metrics(y, s)
    assert set(m) == {"precision", "recall", "f1", "roc_auc", "pr_auc"}
    assert 0.0 <= m["f1"] <= 1.0
    assert m["roc_auc"] > 0.9
    assert m["pr_auc"] > 0.5
    # perfect ranking with a clear gap beats a jumbled one
    m2 = evaluate.compute_metrics(y, s[::-1])
    assert m["pr_auc"] > m2["pr_auc"]


def test_compute_metrics_handles_all_negative():
    y = np.zeros(6, dtype=int)
    s = np.linspace(0, 1, 6)
    m = evaluate.compute_metrics(y, s)  # must not raise (zero_division handled)
    assert m["f1"] == 0.0
    assert m["precision"] == 0.0


def test_permutation_importance_path_for_non_tree_winner(tmp_path):
    rng = np.random.default_rng(0)
    X = rng.normal(size=(300, 5))
    y = (X[:, 0] > 0.5).astype(int)
    clf = LogisticRegression(max_iter=1000, random_state=0).fit(X, y)
    path = evaluate.plot_feature_importance(clf, X, y, [f"f{i}" for i in range(5)], str(tmp_path))
    assert path.endswith("feature_importance.png")
    import os

    assert os.path.exists(path)


def test_plot_bar_comparison_with_cv_columns(tmp_path):
    table = pd.DataFrame(
        {
            "model": ["A", "B"],
            "pr_auc_mean": [0.9, 0.8],
            "pr_auc_std": [0.05, 0.1],
            "roc_auc_mean": [0.95, 0.9],
            "roc_auc_std": [0.01, 0.02],
            "f1_mean": [0.7, 0.6],
            "f1_std": [0.1, 0.05],
        }
    )
    path = evaluate.plot_bar_comparison(table, str(tmp_path))
    import os

    assert os.path.exists(path)
