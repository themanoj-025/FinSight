"""Shared base class and helpers for FinanceFacts tool modules.

This module holds the ``__init__`` logic, config loading, shared math helpers,
and the memoised month series — code that every tool category (facts, risk,
retrieval) needs but should only exist once.
"""

from __future__ import annotations

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
from finance_agent.storage import TransactionStore

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


class _FinanceFactsBase:
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
