"""Similar-transaction retrieval (Phase B.1).

Lets a user — or the agent — answer "why is this flagged — what does it look
like?" with real, grounded comparison cases instead of a black-box score: the
engineered, strictly backward-looking feature vectors (``features.py``) are
indexed as L2-normalized embeddings, and a query transaction returns its k
nearest neighbors with their fraud labels visible.

Backend: ``faiss-cpu`` (``IndexFlatL2``) when installed, otherwise a numpy
exact-L2 brute-force search that is numerically identical for this index size.
The ``faiss`` import is optional (the ``retrieval`` pip extra), so the core
install stays light. The index is rebuilt per process from the current ledger
and gated by ``config.yaml features.faiss_retrieval`` — see
docs/KNOWN_LIMITATIONS.md §20.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger("finance_agent.retrieval")

try:  # faiss-cpu is optional — the numpy fallback is numerically identical.
    import faiss

    _FAISS_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised in the no-extra install
    faiss = None
    _FAISS_AVAILABLE = False


def build_embeddings(features: pd.DataFrame) -> np.ndarray:
    """Column-standardized, L2-normalized row embeddings of a feature matrix.

    Two transforms, each with a purpose:

    1. **Column standardization** (z-score). The raw engineered features are
       heavy-tailed (amounts, counts) and dominated by common values, so raw
       L2 distance is a weak similarity signal — a fraud row's neighbours
       would be ordinary shopping rows. Standardizing by the column mean/std
       puts rare, discriminative signals (wire transfers, fresh regions, big
       category deviations) on an equal footing with common ones. This uses
       global ledger statistics, which is fine for a *display-time* similarity
       tool and never feeds the model — there is no leakage surface into
       training/evaluation.
    2. **L2 normalization.** Puts every transaction on the unit sphere so
       Euclidean distance is a pure directional similarity — a $2 coffee and
       a $200 dinner with the same *shape* land at the same distance scale.

    Rows with zero norm (all-neutral legacy rows) stay at the origin and are
    simply far from everything.
    """
    arr = features.to_numpy(dtype=np.float64)
    mu = arr.mean(axis=0, keepdims=True)
    sd = arr.std(axis=0, keepdims=True)
    sd[sd == 0.0] = 1.0
    z = (arr - mu) / sd
    norms = np.linalg.norm(z, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return (z / norms).astype(np.float32)


class SimilarTransactionIndex:
    """Exact-L2 nearest-neighbor index over transaction feature vectors.

    ``meta`` is a frame whose rows align with the embeddings (one row per
    transaction) and must carry a stable ``transaction_id`` column (the
    original ledger row position) plus whatever display columns the caller
    wants surfaced on the results.
    """

    def __init__(self, embeddings: np.ndarray, meta: pd.DataFrame) -> None:
        if len(embeddings) != len(meta):
            raise ValueError("embeddings and meta must have the same number of rows")
        if "transaction_id" not in meta.columns:
            raise ValueError("meta must carry a transaction_id column")
        self.embeddings = np.asarray(embeddings, dtype=np.float32)
        self.meta = meta.reset_index(drop=True)
        self._faiss_index: Any = None
        if _FAISS_AVAILABLE:
            try:
                assert faiss is not None
                index = faiss.IndexFlatL2(self.embeddings.shape[1])
                index.add(self.embeddings)
                self._faiss_index = index
            except (AttributeError, OSError, ValueError):
                log.warning("faiss index build failed; using the numpy fallback.")
                self._faiss_index = None
        if self._faiss_index is None:
            log.debug("Using the numpy exact-L2 retrieval fallback (faiss-cpu absent).")

    def backend(self) -> str:
        return "faiss" if self._faiss_index is not None else "numpy"

    def find_similar(
        self, query: np.ndarray, k: int, exclude: int | None = None
    ) -> list[dict[str, Any]]:
        """k nearest neighbors of `query` as list[dict] with distance + meta.

        ``exclude`` is a transaction_id (original ledger position) to skip —
        the query transaction itself, when the query is a ledger row.
        """
        q = np.asarray(query, dtype=np.float32).reshape(1, -1)
        limit = k + (1 if exclude is not None else 0)
        if self._faiss_index is not None:
            assert self._faiss_index is not None
            distances, indices = self._faiss_index.search(q, int(limit))
            # Pair each index with its own distance (zip, not enumerate — if
            # faiss ever returns an invalid -1 entry, filtering it must not
            # shift the distances for the entries after it).
            order: list[int] = []
            dist_by_pos: dict[int, float] = {}
            for idx, dist in zip(indices[0], distances[0], strict=True):
                i = int(idx)
                if i >= 0:
                    order.append(i)
                    dist_by_pos[i] = float(dist)
        else:
            # Exact-L2 brute force: numerically identical to IndexFlatL2 for
            # this index size (faiss computes the same squared-L2 distances).
            d = np.sum((self.embeddings - q) ** 2, axis=1)
            order = np.argsort(d, kind="stable")[:limit].tolist()
            distances = d[order].reshape(1, -1)
        out: list[dict[str, Any]] = []
        for rank, pos in enumerate(order):
            tx_id = int(self.meta.iloc[pos]["transaction_id"])
            if exclude is not None and tx_id == exclude:
                continue
            rec = self.meta.iloc[pos].to_dict()
            rec["transaction_id"] = tx_id
            distance = (
                dist_by_pos[pos] if self._faiss_index is not None else float(distances[0][rank])
            )
            rec["distance"] = round(distance, 6)
            out.append(rec)
            if len(out) == k:
                break
        return out


def nearest_neighbors(
    features: pd.DataFrame,
    meta: pd.DataFrame,
    transaction_id: int,
    k: int = 5,
) -> list[dict[str, Any]]:
    """Convenience path: build (or reuse) nothing — one-shot k-NN query.

    ``transaction_id`` is the row's original position in the frame that
    ``features`` was built from (``features`` may be step-reordered by
    ``build_features``, so the caller resolves positions before calling).
    """
    embeddings = build_embeddings(features)
    index = SimilarTransactionIndex(embeddings, meta)
    pos = int(transaction_id)
    if pos < 0 or pos >= len(embeddings):
        raise IndexError(f"transaction_id {pos} out of range [0, {len(embeddings)})")
    return index.find_similar(embeddings[pos : pos + 1], k=k, exclude=pos)


def neighbor_rows(
    neighbors: Iterable[dict[str, Any]],
    keep: tuple[str, ...] = (
        "transaction_id",
        "distance",
        "date",
        "merchant",
        "amount",
        "category",
        "type",
        "isFraud",
        "fraud_archetype",
    ),
) -> list[dict[str, Any]]:
    """Trim raw neighbor records to the JSON-safe display columns.

    ``fraud_archetype`` becomes ``"legitimate"`` for non-fraud rows so the
    LLM and the UI can say "this looks like a known <archetype> pattern"
    with a real label.
    """
    rows: list[dict[str, Any]] = []
    for rec in neighbors:
        row: dict[str, Any] = {}
        for col in keep:
            if col not in rec:
                continue
            value = rec[col]
            if col == "fraud_archetype" and (value is None or not value or value != value):
                # ``not value`` catches ""/None; ``value != value`` catches NaN
                # (which is truthy, so a plain falsy check lets it through).
                value = "legitimate"
            if isinstance(value, (np.integer,)):
                value = int(value)
            elif isinstance(value, (np.floating,)):
                value = round(float(value), 2)
            row[col] = value
        rows.append(row)
    return rows
