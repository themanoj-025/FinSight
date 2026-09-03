"""Risk tools — blended risk scoring, SHAP explanations, and tips.

Every tool returns ``{"summary": str, "data": <jsonable object>}``. The summary is
human-readable prose and the data is structured, so the LLM layer only ever
writes narrative from these outputs and never invents numbers.

This module has no LLM dependency: it is fully offline and unit-testable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np
import pandas as pd

from finance_agent import alerts, rules
from finance_agent._facts_base import (
    _blend_weights,
    _FinanceFactsBase,
    _scored_frame_json,
)
from finance_agent.constants import fmt_money
from finance_agent.features import build_features
from model_bench import models as bench_models

log = logging.getLogger("finance_agent.tools")


class RiskTools(_FinanceFactsBase):
    """Risk provider: blended scoring, SHAP explanations, and tips.

    Inherits shared state and utilities from ``_FinanceFactsBase``.
    """

    # --------------------------------------------------------------- risk scoring
    def _compute_scored_frame(self) -> pd.DataFrame:
        """Score every transaction (rules + optional model) — threshold-independent.

        In rule-only mode (no bundle) the blend is renormalized to the rule score
        directly (weight 1.0) so an obvious fraud case still clears the configured
        threshold; without this the diluted 0.4·rule formula would silently stay
        below the default 0.7 threshold and the scanner would never flag anything.
        """
        risk_cfg = self.cfg.get("risk", {})
        d = rules.rule_risk_flags(self.df, risk_cfg)
        # read_csv turns empty cells (e.g. anomaly_type="") into NaN/<NA>;
        # normalize non-numeric NaN back to "" so the emitted rows are clean
        # and identical to the SQLite path (NOT NULL text columns).
        for col in d.columns:
            if not pd.api.types.is_numeric_dtype(d[col]) and d[col].isna().any():
                d[col] = d[col].fillna("")
        n_total = len(d)
        model_score = np.zeros(n_total)
        iso_norm = np.zeros(n_total)
        if self.bundle:
            X = build_features(self.df)
            arr = X.to_numpy()
            if self.bundle.get("needs_scaling"):
                arr = self.bundle["scaler"].transform(arr)
            model_score = bench_models.predict_scores(self.bundle["best_model"], arr)
            iso_raw = -self.bundle["isolation_forest"].score_samples(
                self.bundle["scaler"].transform(X.to_numpy())
            )
            iso_norm = (iso_raw - iso_raw.min()) / (iso_raw.max() - iso_raw.min() + 1e-9)

        if self.bundle is None:
            risk_score = d["rule_score"].to_numpy(dtype=float)
        else:
            w = _blend_weights(risk_cfg)
            risk_score = (
                w["rules"] * d["rule_score"].to_numpy(dtype=float)
                + w["supervised"] * model_score
                + w["isolation_forest"] * iso_norm
            )

        d["model_score"] = np.round(model_score, 3)
        d["isolation_score"] = np.round(iso_norm, 3)
        d["risk_score"] = np.round(risk_score, 3)
        # Original df position, so a displayed row can be mapped back to its
        # feature row for SHAP-style explanations (the cached JSON blob drops
        # the DataFrame index, so an explicit column is needed).
        d["_row_index"] = np.arange(len(d))

        reasons: list[str] = []
        for _, row in d.iterrows():
            parts = []
            if row["rule_reason"]:
                parts.append(row["rule_reason"])
            if row["model_score"] >= 0.6:
                parts.append(f"model fraud probability {row['model_score']:.2f}")
            if row["isolation_score"] >= 0.85:
                parts.append("unusual pattern vs normal behaviour")
            reasons.append("; ".join(parts) if parts else "")
        d["reason"] = reasons
        return d

    def _shap_explanations(self, rows: pd.DataFrame) -> list[dict[str, Any]]:
        """Per-transaction feature contributions via LightGBM's native SHAP.

        ``pred_contrib=True`` on the LGBMClassifier returns one column per
        feature plus a final bias column; contributions + bias sum exactly to
        the model's log-odds, so the numbers are genuine, not approximations.
        No `shap` package needed — this works for any tree model the bundle
        may carry; for other model classes it degrades to `None` per row.
        """
        if self.bundle is None:
            return []
        model = self.bundle.get("best_model")
        if model is None or not hasattr(model, "predict"):
            return []
        feature_names = list(self.bundle.get("feature_names", []))
        if not feature_names:
            return []
        try:
            X = build_features(self.df)
            # build_features sorts by step and resets the index, so map each
            # original row position (_row_index) to its rank in that sorted
            # order. For the generated data (already step-sorted) this is the
            # identity; the mapping keeps it correct for any future ordering.
            # Stable sort (pandas 2.x quicksort is unstable): transaction ids
            # are step-rank positions, so same-step ties must keep a
            # deterministic order for the retrieval index to match the UI's.
            sorted_positions = self.df.sort_values("step", kind="stable").index
            rank_of: dict[int, int] = {int(orig): k for k, orig in enumerate(sorted_positions)}
            idx = np.asarray(
                [rank_of[int(r)] for r in rows["_row_index"].astype(int).to_numpy()], dtype=int
            )
            arr = X.to_numpy()[idx]
            if self.bundle.get("needs_scaling"):
                arr = self.bundle["scaler"].transform(arr)
            contrib = np.asarray(model.predict(arr, pred_contrib=True), dtype=float)
        except (TypeError, ValueError, NotImplementedError, KeyError):
            # KeyError: a row index missing from the current feature matrix
            # (e.g. a stale on-disk store materialized against an older ledger
            # whose fingerprint collided with the current one). Explanations
            # are a progressive enhancement, not the contract — degrade to
            # "no explanation" rather than 500 the whole scan.
            log.warning(
                "SHAP explanation unavailable for %s (row/feature mismatch); "
                "continuing without explanations.",
                type(model).__name__,
            )
            return []
        out: list[dict[str, Any]] = []
        for orig_pos, c in zip(rows["_row_index"].astype(int).to_numpy(), contrib, strict=True):
            bias = float(c[-1])
            per_feature = [
                {"feature": feature_names[i], "contribution": float(c[i])}
                for i in range(len(feature_names))
            ]
            per_feature.sort(key=lambda x: abs(x["contribution"]), reverse=True)
            out.append(
                {
                    "row_index": int(orig_pos),
                    "method": "TreeSHAP (LightGBM pred_contrib)",
                    "bias": round(bias, 4),
                    "base_probability": round(1.0 / (1.0 + np.exp(-bias)), 4),
                    "top_features": [
                        {**f, "contribution": round(f["contribution"], 4)} for f in per_feature[:8]
                    ],
                    "all_features": [
                        {**f, "contribution": round(f["contribution"], 4)} for f in per_feature
                    ],
                }
            )
        return out

    def risk_scored_transactions(
        self,
        limit: int = 15,
        threshold: float | None = None,
        focal_only: bool = False,
        include_explanations: bool = False,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        """Transactions ranked by blended risk score.

        When a SQLite store is configured (data.store_path) the query runs
        against the materialized `risk_scores` table; otherwise it uses the
        in-memory scored frame (lru-cached per data/model fingerprint). Both
        paths return identical payloads. ``account_type`` narrows the flagged
        rows to one channel (checking / savings / credit) when the ledger
        carries the v2 column; on legacy data it is a no-op.
        """
        risk_cfg = self.cfg.get("risk", {})
        thr = threshold if threshold is not None else float(risk_cfg.get("fraud_threshold", 0.7))
        if self.store is not None:
            result = self._risk_scored_from_store(
                limit, thr, focal_only, include_explanations, account_type
            )
        else:
            result = self._risk_scored_pandas(
                limit, thr, focal_only, include_explanations, account_type
            )
        # Phase E.3 — outbound risk-alert webhook, gated by
        # features.webhook_alerts + alerts.webhook_url and deduplicated per
        # transaction (finance_agent/alerts.py). Best-effort by design: a
        # webhook outage must never break the scan, so this guard is
        # belt-and-braces around a sender that already never raises.
        try:
            alerts.send_risk_alerts(
                result["data"], self.cfg, source="risk_scan", focal_user=self.focal_user
            )
        except (OSError, ConnectionError, TimeoutError):
            log.warning("Risk-alert webhook path failed; scan continues.", exc_info=True)
        return result

    def _risk_scored_pandas(
        self,
        limit: int,
        thr: float,
        focal_only: bool,
        include_explanations: bool,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        """In-memory path (no store configured) — the original implementation."""
        data_mtime, data_size, bundle_mtime, bundle_size = self.risk_fingerprint()
        blob = _scored_frame_json(
            self.config_path, data_mtime, data_size, bundle_mtime, bundle_size
        )
        scored = pd.DataFrame.from_records(json.loads(blob))
        n_total = len(scored)

        flagged_all = scored[scored["risk_score"] >= thr]
        if focal_only:
            # "focal account only" means the *selected* focal user (multi-user).
            flagged_all = flagged_all[flagged_all["nameOrig"] == self.focal_user]
        if account_type and "account_type" in flagged_all.columns:
            flagged_all = flagged_all[flagged_all["account_type"] == account_type]
        # Stable sort: ties keep CSV order so the result is deterministic and
        # identical to the SQL path (ORDER BY risk_score DESC, id ASC).
        rows = flagged_all.sort_values("risk_score", ascending=False, kind="stable").head(limit)
        rule_only = self.bundle is None
        row_dicts, explanations = self._explain_rows(rows, include_explanations, rule_only)
        summary = self._risk_summary(len(flagged_all), n_total, thr, len(rows))
        return {
            "summary": summary,
            "data": {
                "threshold": thr,
                "rows": row_dicts,
                "total_scored": n_total,
                "flagged_count": len(flagged_all),
                "scoring_mode": "rule_only" if rule_only else "blended",
                "explanations_available": bool(explanations),
            },
        }

    def _risk_scored_from_store(
        self,
        limit: int,
        thr: float,
        focal_only: bool,
        include_explanations: bool,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        """Store path: materialize once per fingerprint, then SQL point queries.

        The expensive computation (`_compute_scored_frame`: rules + features +
        model inference over the whole ledger) only runs when the data or the
        model bundle fingerprint changed; every threshold/focal/limit variation
        afterwards is a plain SQL query against the materialized table.
        """
        store = self.store
        assert store is not None
        data_mtime, data_size, bundle_mtime, bundle_size = self.risk_fingerprint()
        risk_fp = f"{data_mtime}:{data_size}:{bundle_mtime}:{bundle_size}"
        csv_fp = f"{data_mtime}:{data_size}"
        if not store.is_risk_materialized(risk_fp):
            store.sync_from_frame(self.df, csv_fp)
            scored = self._compute_scored_frame()
            store.materialize_risk_scores(risk_fp, scored)

        rows = store.risk_scores(
            threshold=thr,
            focal_only=focal_only,
            limit=limit,
            focal_user=self.focal_user,
            account_type=account_type,
        )
        n_total = store.total_rows() or len(self.df)
        flagged_count = store.flagged_count(
            threshold=thr,
            focal_only=focal_only,
            focal_user=self.focal_user,
            account_type=account_type,
        )
        rule_only = self.bundle is None
        row_dicts, explanations = self._explain_rows(rows, include_explanations, rule_only)
        summary = self._risk_summary(flagged_count, n_total, thr, len(rows))
        return {
            "summary": summary,
            "data": {
                "threshold": thr,
                "rows": row_dicts,
                "total_scored": n_total,
                "flagged_count": flagged_count,
                "scoring_mode": "rule_only" if rule_only else "blended",
                "explanations_available": bool(explanations),
            },
        }

    def _explain_rows(
        self, rows: pd.DataFrame, include_explanations: bool, rule_only: bool
    ) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
        """Attach SHAP explanations keyed by `_row_index`; returns (row_dicts, by_id).

        The `explanation` key is only added when explicitly requested, so the
        default (agent/narrator) payload stays lean — no null-key noise.
        """
        explanations: dict[int, dict[str, Any]] = {}
        if include_explanations and not rule_only and not rows.empty:
            for expl in self._shap_explanations(rows):
                explanations[expl["row_index"]] = expl
        row_dicts: list[dict[str, Any]] = []
        for rec in rows.to_dict(orient="records"):
            ridx = int(rec.pop("_row_index", -1))
            # The original ledger position — surfaced on EVERY row so the UI
            # can feed a flagged row into find_similar_transactions (Phase
            # B.1) and the risk-alert webhook (Phase E.3) has a stable,
            # dedup-able transaction id regardless of path. One int per row
            # is negligible payload noise on the agent/narrator path.
            rec["row_index"] = ridx
            if include_explanations:
                rec["explanation"] = explanations.get(ridx)
            row_dicts.append(rec)
        return row_dicts, explanations

    @staticmethod
    def _risk_summary(flagged_count: int, n_total: int, thr: float, shown: int) -> str:
        if flagged_count == 0:
            return f"No transactions score above {thr:.2f} this period — your account looks clean."
        pct = 100.0 * flagged_count / n_total
        return (
            f"{flagged_count} of {n_total} transactions ({pct:.1f}%) score above "
            f"{thr:.2f}; showing the top {shown}."
        )

    def top_tips(self) -> dict[str, Any]:
        # Advice thresholds are tunable in config.yaml (advice.*) and echoed in
        # the payload so any number in the tip text is grounded in the tool
        # output (never a hardcoded constant that could drift from config).
        advice_cfg = self.cfg.get("advice") or {}
        savings_goal = float(advice_cfg.get("savings_rate_goal", 0.20))
        sub_limit = float(advice_cfg.get("subscription_ratio_limit", 0.05))
        health = rules.compute_financial_health(self._focal())
        forecast = self.forecast_next_month()
        risk = self.risk_scored_transactions(limit=5)
        flagged = [r for r in risk["data"]["rows"] if r["risk_score"] >= risk["data"]["threshold"]]
        tips: list[str] = []
        if health["savings_rate"] < savings_goal:
            tips.append(
                f"Your savings rate is {health['savings_rate'] * 100:.1f}% — "
                f"pushing it toward {savings_goal * 100:.0f}% would lift your health "
                "score fastest."
            )
        elif health["components"]["subscription_ratio"] > sub_limit:
            tips.append(
                f"Subscriptions are ~{health['components']['subscription_ratio'] * 100:.1f}% "
                "of income; trimming one could add meaningful monthly savings."
            )
        else:
            tips.append(
                f"Healthy savings rate of {health['savings_rate'] * 100:.1f}% — keep it up."
            )
        if flagged:
            tips.append(
                f"{len(flagged)} transactions were flagged suspicious this period — "
                "review them in the Fraud & Anomaly Detection page."
            )
        tips.append(
            f"Next month projects {fmt_money(forecast['data'].get('forecast_expenses', 0))} "
            "in expenses; keep ~3-6 months of expenses as a buffer."
        )
        return {
            "summary": "\n".join(f"{i + 1}. {t}" for i, t in enumerate(tips[:3])),
            "data": {
                "tips": tips[:3],
                "goals": {
                    "savings_rate": round(savings_goal, 4),
                    "subscription_ratio": round(sub_limit, 4),
                },
            },
        }
