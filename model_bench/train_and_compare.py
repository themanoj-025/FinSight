from model_bench.train_helpers import *

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
