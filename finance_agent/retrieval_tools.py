"""Retrieval tools — similar-transaction search via FAISS / exact-L2.

Every tool returns ``{"summary": str, "data": <jsonable object>}``. The summary is
human-readable prose and the data is structured, so the LLM layer only ever
writes narrative from these outputs and never invents numbers.

This module has no LLM dependency: it is fully offline and unit-testable.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from finance_agent._facts_base import _FinanceFactsBase
from finance_agent.constants import fmt_money
from finance_agent.features import build_features
from finance_agent.retrieval import SimilarTransactionIndex, build_embeddings, neighbor_rows

log = logging.getLogger("finance_agent.tools")


class RetrievalTools(_FinanceFactsBase):
    """Retrieval provider: similar-transaction search via FAISS / exact-L2.

    Inherits shared state and utilities from ``_FinanceFactsBase``.
    """

    # -------------------------------------------- similar-transaction retrieval
    def _retrieval_index(self) -> tuple[Any, pd.Index] | None:
        """Feature-space index over the whole ledger (Phase B.1), memoized per
        data fingerprint. Returns None when the `faiss_retrieval` flag is off
        (or the feature matrix can't be aligned to the ledger).

        The engineered feature matrix (``features.py``) is standardized and
        L2-normalized, then indexed with FAISS (``IndexFlatL2``) or an exact-L2
        numpy fallback — see ``finance_agent/retrieval.py``. Row ``k`` of the
        returned index corresponds to the ``k``-th row of
        ``build_features(self.df)``; the ``sorted_positions`` index maps
        feature rows back to original ledger row positions.
        """
        features_cfg = self.cfg.get("features") or {}
        if not features_cfg.get("faiss_retrieval", True):
            return None
        d_mtime, d_size, *_ = self.risk_fingerprint()
        fp = (d_mtime, d_size)
        if self._retrieval_cache is not None and self._retrieval_cache[0] == fp:
            return self._retrieval_cache[1]
        features_df = build_features(self.df)
        if len(features_df) != len(self.df):
            # Alignment guard: the feature matrix must have one row per ledger
            # row (same assumption as the SHAP path). Degrade visibly rather
            # than returning misaligned neighbours.
            log.warning(
                "Similar-transaction retrieval disabled: build_features returned %d rows "
                "for a %d-row ledger.",
                len(features_df),
                len(self.df),
            )
            return None
        # Stable sort so step-rank transaction ids match retrieval.py's index
        # on every pandas version (pandas 2.x quicksort is unstable).
        sorted_positions = self.df.sort_values("step", kind="stable").index
        meta = self.df.loc[sorted_positions].copy()
        meta["transaction_id"] = np.asarray(sorted_positions, dtype=int)
        index = SimilarTransactionIndex(build_embeddings(features_df), meta)
        self._retrieval_cache = (fp, (index, sorted_positions))
        return self._retrieval_cache[1]

    def _query_row(self, tid: int) -> dict[str, Any]:
        """JSON-safe display row for the query transaction itself."""
        keep = ("date", "merchant", "amount", "category", "type", "isFraud", "fraud_archetype")
        rec = self.df.loc[tid]
        rows = neighbor_rows([rec.to_dict()], keep=keep)
        return rows[0] if rows else {"transaction_id": int(tid)}

    def _top_risk_row_index(self) -> int | None:
        """Original ledger position of the highest-risk flagged transaction,
        or None when nothing is flagged above the configured threshold."""
        thr = float(self.cfg.get("risk", {}).get("fraud_threshold", 0.7))
        # include_explanations=False on purpose (F.5 load-test finding): this
        # helper only reads `row_index`, but TreeSHAP runs per row (~1.3s on
        # the demo tier) — the default similar-transactions call was paying
        # that cost twice over. Explanations are computed lazily by the caller
        # that actually renders them.
        result = self.risk_scored_transactions(limit=1, threshold=thr, include_explanations=False)
        rows = result["data"]["rows"]
        if rows and "row_index" in rows[0]:
            return int(rows[0]["row_index"])
        return None

    def find_similar_transactions(
        self, transaction_id: int | None = None, k: int = 5
    ) -> dict[str, Any]:
        """The k transactions most similar to `transaction_id` in feature space.

        Phase B.1 — "why is this flagged, what does it look like?": the
        strictly backward-looking feature vectors are L2-normalized and indexed
        (faiss ``IndexFlatL2``, or the exact-L2 numpy fallback); returned
        neighbors carry their ``fraud_archetype`` labels (or ``"legitimate"``)
        so a user — or the agent — can compare a flag against real, grounded
        cases instead of a black-box score. Gated by
        ``config.yaml features.faiss_retrieval``.

        ``transaction_id`` is the transaction's original ledger row position;
        when omitted, the highest-risk flagged transaction is used. ``k`` is
        clamped to [1, 20].
        """
        k = max(1, min(int(k or 5), 20))
        cached = self._retrieval_index()
        if cached is None:
            return {
                "summary": (
                    "Similar-transaction retrieval is disabled — set "
                    "config.yaml features.faiss_retrieval to true to enable it."
                ),
                "data": {"enabled": False, "neighbors": []},
            }
        index, sorted_positions = cached
        if transaction_id is None:
            tid = self._top_risk_row_index()
            if tid is None:
                return {
                    "summary": (
                        "Nothing is flagged above the configured threshold — pass an explicit "
                        "transaction_id to see its nearest neighbours."
                    ),
                    "data": {"enabled": True, "transaction_id": None, "neighbors": []},
                }
        else:
            tid = int(transaction_id)
        positions = np.asarray(sorted_positions, dtype=int)
        hits = np.flatnonzero(positions == tid)
        if len(hits) == 0:
            return {
                "summary": (
                    f"No transaction with row position {tid} in the ledger — pass a "
                    "valid transaction id."
                ),
                "data": {"enabled": True, "transaction_id": tid, "neighbors": []},
            }
        pos = int(hits[0])
        neighbors = index.find_similar(index.embeddings[pos : pos + 1], k=k, exclude=tid)
        rows = neighbor_rows(neighbors)
        query_rec = self._query_row(tid)
        fraud_n = sum(1 for r in rows if r.get("fraud_archetype") != "legitimate")
        summary = (
            f"{len(rows)} transactions most similar to "
            f"{query_rec.get('date', '')} {query_rec.get('merchant', '')} "
            f"({fmt_money(query_rec.get('amount', 0))}) in feature space — "
            f"{fraud_n} of them are known-fraud patterns."
        )
        return {
            "summary": summary,
            "data": {
                "enabled": True,
                "backend": index.backend(),
                "transaction_id": tid,
                "query": query_rec,
                "neighbors": rows,
            },
        }
