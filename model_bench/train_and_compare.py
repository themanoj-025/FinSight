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

The implementation (imports, helpers, and ``main()``) lives in
:mod:`model_bench.train_helpers`; this module keeps the historical CLI path
working and exposes the same names for ``from model_bench.train_and_compare
import hpo_provenance``-style imports.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent)
)  # allow `python model_bench/train_and_compare.py`

from model_bench.train_helpers import *
from model_bench.train_helpers import main

if __name__ == "__main__":
    main()
