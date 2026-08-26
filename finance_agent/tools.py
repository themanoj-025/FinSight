"""Facts layer — deterministic Python + the trained model.

Every tool returns `{"summary": str, "data": <jsonable object>}`. The summary is
human-readable prose and the data is structured, so the LLM layer only ever
writes narrative from these outputs and never invents numbers.

This module has no LLM dependency: it is fully offline and unit-testable.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any

import joblib
import numpy as np
import pandas as pd
import yaml

from finance_agent import alerts, rules
from finance_agent.bundle_security import verify_bundle
from finance_agent.config_schema import validate_config
from finance_agent.constants import CREDIT_TYPES, fmt_money
from finance_agent.features import build_features
from finance_agent.retrieval import SimilarTransactionIndex, build_embeddings, neighbor_rows
from finance_agent.storage import TransactionStore
from model_bench import models as bench_models

log = logging.getLogger("finance_agent.tools")

# Blend weights are read live from config.yaml so prose and computation can
# never drift apart; the defaults mirror the shipped config.
DEFAULT_BLEND = {"rules": 0.4, "supervised": 0.3, "isolation_forest": 0.3}


def load_config(config_path: str = "config.yaml") -> dict[str, Any]:
    with open(config_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return validate_config(cfg)


def _blend_weights(risk_cfg: dict[str, Any]) -> dict[str, float]:
    blend = risk_cfg.get("blend", DEFAULT_BLEND)
    return {
        "rules": float(blend.get("rules", DEFAULT_BLEND["rules"])),
        "supervised": float(blend.get("supervised", DEFAULT_BLEND["supervised"])),
        "isolation_forest": float(blend.get("isolation_forest", DEFAULT_BLEND["isolation_forest"])),
    }


def blend_description(risk_cfg: dict[str, Any], rule_only: bool) -> str:
    """Human-readable description of the current risk blend, built from config.

    Never hardcode the 0.4/0.3/0.3 prose anywhere else — call this instead so the
    displayed text always matches the actual computation.
    """
    if rule_only:
        return (
            "Scoring is rule-based only (no trained model bundle on disk) — "
            "risk = rule score, weights in config.yaml risk.blend."
        )
    w = _blend_weights(risk_cfg)
    return (
        f"Scores blend rules ({w['rules'] * 100:.0f}%), the supervised model "
        f"({w['supervised'] * 100:.0f}%), and an isolation forest "
        f"({w['isolation_forest'] * 100:.0f}%)."
    )


# ------------------------------------------------------------------ shared math
# These column masks / aggregations are the single source of truth for the
# income/savings/expenses figures. Both the facts tools (`FinanceFacts`) and
# the Streamlit dashboard (`app/common.py::monthly_table`) delegate here so
# the numbers the agent quotes and the numbers the UI shows can never drift
# apart (C.2.5).


def income_mask(d: pd.DataFrame) -> pd.Series:
    """Cash-in that counts as income: salary + non-credit credits on the primary
    (checking) channel. On the multi-account model the credit card's autopay
    inflow and the savings account's auto-transfer inflow are movements between
    the persona's own accounts, not income.
    """
    mask = d["type"].isin(CREDIT_TYPES)
    if "account_type" in d.columns:
        mask &= d["account_type"] == "checking"
    return mask


def savings_out_mask(d: pd.DataFrame) -> pd.Series:
    """Transfers to the persona's own savings account (money set aside)."""
    return (d["type"] == "TRANSFER") & (d["category"] == "savings")


def income_savings_expenses(d: pd.DataFrame) -> tuple[float, float, float]:
    """(income, savings_out, expenses) for one frame.

    Expenses use ``rules.expense_rows`` (which also excludes the credit autopay
    debit), so credit-card spend is counted once.
    """
    income = float(d.loc[income_mask(d), "amount"].sum())
    savings_out = float(d.loc[savings_out_mask(d), "amount"].sum())
    expenses = float(d.loc[rules.expense_rows(d), "amount"].sum())
    return income, savings_out, expenses


def monthly_income_expenses(d: pd.DataFrame) -> pd.DataFrame:
    """Per-month (income, expenses, net) rows for the dashboard table.

    Every figure delegates to ``income_savings_expenses`` so the dashboard and
    the facts tools compute identical numbers (C.2.5).
    """
    d = d.copy()
    d["_month"] = pd.to_datetime(d["datetime"]).dt.strftime("%Y-%m")
    rows: list[dict[str, Any]] = []
    for month, g in d.groupby("_month"):
        income, savings, expenses = income_savings_expenses(g)
        rows.append(
            {
                "month": month,
                "income": round(income, 2),
                "expenses": round(expenses, 2),
                "net": round(income - expenses - savings, 2),
            }
        )
    return pd.DataFrame(rows).sort_values("month")


@lru_cache(maxsize=16)
def _scored_frame_json(
    cfg_path: str,
    data_mtime_ns: int,
    data_size: int,
    bundle_mtime_ns: int,
    bundle_size: int,
) -> str:
    """Expensive path: rules + features + model inference for the whole ledger.

    Keyed on the data/bundle fingerprints so moving a threshold slider re-filters
    cached scores instead of recomputing everything on every Streamlit rerun
    (2.8). Returns the fully-scored frame as JSON records.
    """
    facts = FinanceFacts(cfg_path)
    return facts._compute_scored_frame().to_json(orient="records")


class FinanceFacts:
    """Stateless-ish facts provider backed by the transaction table + model bundle.

    Multi-user: the ledger may contain several focal users (config
    `data.focal_users`); `focal_user` selects which one the per-user tools
    (monthly summary, category breakdown, health, …) report on. Defaults to
    config `data.focal_user` (or the first focal user).
    """

    def __init__(self, config_path: str = "config.yaml", focal_user: str | None = None) -> None:
        self.config_path = config_path
        self.cfg = load_config(config_path)
        data_cfg = self.cfg.get("data", {})
        data_path = str(data_cfg.get("path", "data/transactions.csv"))
        self.df = pd.read_csv(data_path)
        configured = [str(u) for u in data_cfg.get("focal_users") or []]
        self.focal_users = configured or [str(data_cfg.get("focal_user", "U_Alex"))]
        self.focal_user = (
            str(focal_user)
            if focal_user
            else str(data_cfg.get("focal_user") or self.focal_users[0])
        )
        if self.focal_user not in self.focal_users:
            self.focal_users.insert(0, self.focal_user)
        self.bundle: dict[str, Any] | None = None
        bundle_path = str(
            self.cfg.get("model_bench", {}).get(
                "bundle_path", "model_bench/risk_model_bundle.joblib"
            )
        )
        # SECURITY NOTE: joblib bundles are pickles — loading one executes
        # arbitrary code. The training pipeline signs each bundle (C.2.4) and
        # we verify the HMAC-SHA256 signature BEFORE joblib.load: a tampered,
        # swapped, or corrupt bundle is refused and the app degrades to
        # rule-only mode instead of deserializing untrusted bytes. Only ever
        # load bundles produced by this project's own training pipeline
        # (model_bench/train_and_compare.py).
        if os.path.exists(bundle_path):
            try:
                ok, reason = verify_bundle(bundle_path)
                if not ok:
                    log.error("Refusing to load model bundle %s: %s", bundle_path, reason)
                else:
                    if reason != "signature OK":
                        log.warning("Model bundle %s: %s", bundle_path, reason)
                    self.bundle = joblib.load(bundle_path)
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("Could not load model bundle %s; continuing rule-only.", bundle_path)
        # Optional SQLite persistence layer (config data.store_path). When set,
        # the expensive risk-scoring path runs once per (data, model)
        # fingerprint and results are materialized in the store; the interactive
        # scan then becomes a SQL point query instead of re-filtering a full
        # in-memory scored frame on every rerun. See finance_agent/storage.py.
        store_path = str(self.cfg.get("data", {}).get("store_path", "") or "")
        self.store: TransactionStore | None = TransactionStore(store_path) if store_path else None
        # Memoized (data fingerprint, retrieval index) pair — see
        # `_retrieval_index` (Phase B.1).
        self._retrieval_cache: tuple[tuple[int, int], Any] | None = None
        # Memoized month column (F.5 load-test finding): `_month_key` and
        # `_for_month` both ran `pd.to_datetime(...).dt.strftime` over the full
        # ledger on every call (~200 ms each, 2-3x per request) — that alone
        # pushed the monthly tools' warm p95 well past the 200 ms SLO in
        # docs/technical/SLOs.md. Computed once per instance; the ledger is
        # immutable for the life of a FinanceFacts, so this never goes stale.
        self._month_series: pd.Series | None = None

    def rule_only(self) -> bool:
        """True when scoring falls back to rules (no usable model bundle)."""
        return self.bundle is None

    def risk_fingerprint(self) -> tuple[int, int, int, int]:
        """(mtime, size) of the data CSV and the model bundle, for cache keys."""
        data_path = str(self.cfg.get("data", {}).get("path", "data/transactions.csv"))
        bundle_path = str(
            self.cfg.get("model_bench", {}).get(
                "bundle_path", "model_bench/risk_model_bundle.joblib"
            )
        )
        d = os.stat(data_path)
        if os.path.exists(bundle_path):
            b = os.stat(bundle_path)
            return d.st_mtime_ns, d.st_size, b.st_mtime_ns, b.st_size
        return d.st_mtime_ns, d.st_size, 0, 0

    # ------------------------------------------------------------------ utils
    def account_types(self) -> list[str]:
        """Account types of the selected persona's accounts (dashboard filter).

        Scoped to the focal user so background accounts never surface in the
        dashboard's account switcher; degrades to ["checking"] on legacy data.
        """
        if "account_type" not in self.df.columns:
            return ["checking"]
        if "persona_id" in self.df.columns:
            d = self.df[self.df["persona_id"] == self.focal_user]
        else:
            d = self.df[self.df["nameOrig"] == self.focal_user]
        return [str(t) for t in sorted(d["account_type"].dropna().unique())]

    def _focal(self, account_type: str | None = None) -> pd.DataFrame:
        """Rows for the selected focal user, optionally narrowed to an account type.

        The default (``None`` / ``"checking"``) returns the user's primary
        checking account — the classic view. ``"all"`` returns every account
        that belongs to the persona (checking + savings + credit), and a
        specific type (``"savings"`` / ``"credit"``) narrows to that channel.
        On legacy data without the v2 columns everything degrades to the
        primary account.
        """
        has_v2 = "persona_id" in self.df.columns and "account_type" in self.df.columns
        if not has_v2:
            return self.df[self.df["nameOrig"] == self.focal_user].copy()
        by_persona = self.df[self.df["persona_id"] == self.focal_user]
        if account_type in (None, "", "checking"):
            return by_persona[by_persona["account_type"] == "checking"].copy()
        if account_type == "all":
            return by_persona.copy()
        return by_persona[by_persona["account_type"] == account_type].copy()

    def _income_savings_expenses(self, d: pd.DataFrame) -> tuple[float, float, float]:
        """(income, savings_out, expenses) — delegates to the shared module
        helper (C.2.5) so the facts layer and the app compute identical numbers."""
        return income_savings_expenses(d)

    def _all_months(self) -> pd.Series:
        """``YYYY-MM`` label for every row of the full ledger (memoized)."""
        if self._month_series is None:
            self._month_series = pd.to_datetime(self.df["datetime"]).dt.strftime("%Y-%m")
        return self._month_series

    def _month_key(self, month: str | None) -> str:
        months = sorted(self._all_months().unique())
        return month or (months[-1] if months else "")

    def _for_month(self, month: str | None, account_type: str | None = None) -> pd.DataFrame:
        d = self._focal(account_type=account_type)
        # `_all_months` is indexed by the full-frame row order — the focal
        # frame shares that order (a filtered copy), so align on the index.
        d["_month"] = self._all_months().reindex(d.index).values
        key = self._month_key(month)
        return d[d["_month"] == key]

    # --------------------------------------------------------------- tools
    def monthly_summary(
        self, month: str | None = None, account_type: str | None = None
    ) -> dict[str, Any]:
        d = self._for_month(month, account_type=account_type)
        key = self._month_key(month)
        if d.empty:
            return {"summary": f"No transactions recorded for {key}.", "data": {}}
        income, savings_out, expenses = self._income_savings_expenses(d)
        net = income - expenses - savings_out
        savings_rate = net / income if income > 0 else 0.0
        top = (
            d.loc[rules.expense_rows(d)]
            .groupby("category")["amount"]
            .sum()
            .sort_values(ascending=False)
        )
        top_cat = str(top.index[0]) if not top.empty else "none"
        summary = (
            f"{key}: income {fmt_money(income)}, expenses {fmt_money(expenses)}, "
            f"net {fmt_money(net)}, savings rate {savings_rate * 100:.1f}%. "
            f"Largest expense category: {top_cat}."
        )
        return {
            "summary": summary,
            "data": {
                "month": key,
                "income": round(income, 2),
                "expenses": round(expenses, 2),
                "net": round(net, 2),
                "savings_rate": round(savings_rate, 4),
                "top_category": top_cat,
                "transaction_count": int(len(d)),
            },
        }

    def category_breakdown(
        self, month: str | None = None, account_type: str | None = None
    ) -> dict[str, Any]:
        d = self._for_month(month, account_type=account_type)
        key = self._month_key(month)
        spend = d.loc[rules.expense_rows(d)].groupby("category")["amount"].sum()
        spend = spend[spend.index != "savings"] if "savings" in spend.index else spend
        spend = spend.sort_values(ascending=False)
        total = float(spend.sum())
        rows: list[dict[str, Any]] = [
            {
                "category": str(c),
                "amount": round(float(a), 2),
                "share": round(float(a) / total, 3) if total else 0.0,
            }
            for c, a in spend.items()
        ]
        summary = (
            f"{key} spending by category: "
            + ", ".join(f"{r['category']} {fmt_money(r['amount'])}" for r in rows[:4])
            + (f" (total {fmt_money(total)})." if total else " (no spending).")
        )
        return {"summary": summary, "data": {"month": key, "total": round(total, 2), "rows": rows}}

    def budget_status(
        self, month: str | None = None, account_type: str | None = None
    ) -> dict[str, Any]:
        """Per-category monthly spend vs. configured budget goals.

        Goals come from `config.yaml budgets.monthly` (category -> amount). A
        category with no goal is not tracked. Returns spend, goal, and the
        fraction of the goal used, so the UI can render progress bars.
        """
        d = self._for_month(month, account_type=account_type)
        key = self._month_key(month)
        goals = (self.cfg.get("budgets") or {}).get("monthly") or {}
        if not goals:
            return {
                "summary": "No budget goals configured (add `budgets.monthly` to config.yaml).",
                "data": {"month": key, "configured": False, "rows": []},
            }
        spend = d.loc[rules.expense_rows(d)].groupby("category")["amount"].sum()
        spend = spend[spend.index != "savings"] if "savings" in spend.index else spend
        rows: list[dict[str, Any]] = []
        for cat, goal in goals.items():
            goal_f = float(goal)
            spent_f = round(float(spend.get(cat, 0.0)), 2)
            pct = spent_f / goal_f if goal_f > 0 else 0.0
            rows.append(
                {
                    "category": str(cat),
                    "goal": round(goal_f, 2),
                    "spent": spent_f,
                    "pct": round(pct, 3),
                    "over": pct > 1.0,
                }
            )
        rows.sort(key=lambda r: r["pct"], reverse=True)
        over = [r["category"] for r in rows if r["over"]]
        total_goal = round(sum(r["goal"] for r in rows), 2)
        total_spent = round(sum(r["spent"] for r in rows), 2)
        if over:
            summary = (
                f"{key} budget tracker: {len(over)} of {len(rows)} categories over "
                f"their goal ({', '.join(over)}); overall {total_spent:.0f} of "
                f"{total_goal:.0f} ({total_spent / total_goal * 100:.0f}%) used."
            )
        else:
            summary = (
                f"{key} budget tracker: all {len(rows)} tracked categories within "
                f"goal; overall {total_spent:.0f} of {total_goal:.0f} "
                f"({total_spent / total_goal * 100:.0f}%) used."
            )
        return {
            "summary": summary,
            "data": {
                "month": key,
                "configured": True,
                "rows": rows,
                "total_goal": total_goal,
                "total_spent": total_spent,
            },
        }

    def recurring_payments(self) -> dict[str, Any]:
        rec = rules.detect_recurring_payments(self._focal())
        if rec.empty:
            return {
                "summary": "No recurring payments detected with stable amounts and intervals.",
                "data": {"rows": []},
            }
        monthly = float(rec["amount"].sum())
        preview = list(zip(rec["merchant"], rec["amount"], rec["interval_days"], strict=True))[:5]
        summary = (
            f"{len(rec)} recurring payments totaling ~{fmt_money(monthly)}/month: "
            + ", ".join(f"{m} {fmt_money(a)} every {i:.0f} days" for m, a, i in preview)
        )
        return {
            "summary": summary,
            "data": {"rows": rec.to_dict(orient="records"), "monthly_total": round(monthly, 2)},
        }

    def spend_spikes(self) -> dict[str, Any]:
        spikes = rules.detect_spend_spikes(self.df)
        if spikes.empty:
            return {"summary": "No category spending spikes detected.", "data": {"rows": []}}
        rows = spikes[["step", "date", "category", "merchant", "amount"]].to_dict(orient="records")
        total = float(spikes["amount"].sum())
        cats = ", ".join(sorted(set(spikes["category"])))
        summary = (
            f"{len(spikes)} transactions in a spending spike ({cats}, {fmt_money(total)} total)."
        )
        return {"summary": summary, "data": {"rows": rows, "total": round(total, 2)}}

    def financial_health(self) -> dict[str, Any]:
        health = rules.compute_financial_health(self._focal())
        summary = (
            f"Financial health score {health['score']}/100 — savings rate "
            f"{health['savings_rate'] * 100:.1f}%, ~{health['buffer_months']:.1f} "
            f"months of expenses in buffer."
        )
        return {"summary": summary, "data": health}

    def forecast_next_month(self) -> dict[str, Any]:
        d = self._focal()
        d["_month"] = pd.to_datetime(d["datetime"]).dt.strftime("%Y-%m")
        d["_amount"] = d["amount"]
        monthly = (
            d.groupby("_month")
            .apply(
                lambda g: pd.Series(
                    {
                        "income": float(g.loc[g["type"].isin(CREDIT_TYPES), "amount"].sum()),
                        "expenses": float(g.loc[rules.expense_rows(g), "amount"].sum()),
                    }
                ),
                include_groups=False,
            )
            .sort_index()
        )
        if monthly.empty:
            return {"summary": "Not enough history to forecast.", "data": {}}
        months = list(monthly.index)
        incomes = monthly["income"].to_numpy(dtype=float)
        expenses = monthly["expenses"].to_numpy(dtype=float)
        n = len(months)
        slope = lambda v: float(np.polyfit(np.arange(n), v, 1)[0]) if n >= 2 else 0.0  # noqa: E731
        f_income = max(0.0, float(incomes[-1]) + slope(incomes))
        f_expenses = max(0.0, float(expenses[-1]) + slope(expenses))
        trend = (
            "rising" if slope(expenses) > 0 else ("falling" if slope(expenses) < 0 else "stable")
        )
        summary = (
            f"Next month projection: income ~{fmt_money(f_income)}, expenses "
            f"~{fmt_money(f_expenses)}, net ~{fmt_money(f_income - f_expenses)} "
            f"(expense trend {trend})."
        )
        return {
            "summary": summary,
            "data": {
                "forecast_income": round(f_income, 2),
                "forecast_expenses": round(f_expenses, 2),
                "forecast_net": round(f_income - f_expenses, 2),
                "trend": trend,
                "history": [
                    {"month": m, "income": round(float(i), 2), "expenses": round(float(e), 2)}
                    for m, i, e in zip(months, incomes, expenses, strict=True)
                ],
            },
        }

    # ------------------------------------------------- blended risk scoring
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
        n_total = int(len(d))
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
        n_total = int(len(scored))

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
        summary = self._risk_summary(int(len(flagged_all)), n_total, thr, len(rows))
        return {
            "summary": summary,
            "data": {
                "threshold": thr,
                "rows": row_dicts,
                "total_scored": n_total,
                "flagged_count": int(len(flagged_all)),
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
        n_total = store.total_rows() or int(len(self.df))
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


def tool_result_payload(result: dict[str, Any]) -> str:
    """Compact JSON for an LLM tool_result — numbers only ever come from here."""
    return json.dumps(result["data"], default=str)
