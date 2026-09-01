"""Deterministic, explainable rule detectors — no ML involved.

These hand-written rules encode the structural fraud/anomaly signatures that a
human auditor would look for. They are unit-tested against crafted edge cases
(see tests/test_rules.py) and feed the explainable half of the blended risk
score consumed by the agent and the app.

All detectors are vectorized (no per-row Python loops) so they stay fast as the
ledger grows; a 100k-row scan runs in well under a second.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from finance_agent.constants import SPENDING_CATEGORIES

DUPLICATE_TYPES = {"SHOP", "PAYMENT", "SUBSCRIPTION", "DEBIT"}
EMPTY_DRAIN_COLS = ["step", "amount", "nameOrig", "nameDest", "category", "merchant", "reason"]


def expense_rows(d: pd.DataFrame) -> pd.Series:
    """Boolean mask over `d` marking spending (debit) rows.

    Excludes income credits (SALARY, CASH_IN), savings transfers, and
    credit-card autopay transfers (category ``credit``) so expense totals are
    computed from actual debits rather than by subtracting from total flow
    (which would misclassify credits such as CASH_IN as spending). The credit
    autopay is a liability settlement, not an expense — the underlying spend is
    the credit-account SHOP rows, which are counted here.
    """
    return (
        (d["type"] != "SALARY")
        & (d["type"] != "CASH_IN")
        & ~((d["type"] == "TRANSFER") & (d["category"] == "savings"))
        & ~((d["type"] == "TRANSFER") & (d["category"] == "credit"))
    )


def detect_duplicate_charges(
    df: pd.DataFrame, window_days: float = 2.0, amount_tol: float = 0.01
) -> pd.DataFrame:
    """Flag the later occurrence of a same-merchant, near-equal-amount charge.

    Only spending transactions of the focal user are considered, so that
    background noise never pollutes the user's own ledger.
    """
    d = df[df["is_focal_user"] & df["type"].isin(DUPLICATE_TYPES)].sort_values("step").copy()
    if d.empty:
        return pd.DataFrame(columns=["step", "amount", "merchant", "category", "reason"])
    d["_amt"] = d["amount"].round(2)
    key = d["merchant"].astype(str) + "|" + d["_amt"].astype(str)
    grouped = d.groupby(key, sort=False)
    d["_prev_step"] = grouped["step"].shift(1)
    gap_hours = d["step"] - d["_prev_step"]
    dup_mask = (gap_hours >= 0) & (gap_hours <= window_days * 24)
    flagged = d[dup_mask]
    return flagged[["step", "amount", "merchant", "category"]].assign(reason="duplicate_charge")


def detect_balance_drain(
    df: pd.DataFrame, window_hours: float = 6.0, min_amount: float = 500.0, drain_ratio: float = 0.5
) -> pd.DataFrame:
    """Flag TRANSFERs that drain an account below `drain_ratio` of its prior
    balance and are followed by a matching CASH_OUT from the destination.

    Implemented as a single vectorized ``pd.merge_asof`` join (per account,
    forward in time within `window_hours`) instead of the previous O(T×C)
    pairwise scan: each qualifying transfer is paired with the nearest
    subsequent CASH_OUT from its destination account.
    """
    d = df.sort_values("step").copy()
    transfers = d[
        (d["type"] == "TRANSFER")
        & (d["amount"] >= min_amount)
        & (d["oldbalanceOrg"] > 0)
        & (d["newbalanceOrig"] <= drain_ratio * d["oldbalanceOrg"])
    ]
    empty = pd.DataFrame(columns=EMPTY_DRAIN_COLS)
    if transfers.empty:
        return empty
    cashouts = d[d["type"] == "CASH_OUT"]
    if cashouts.empty:
        return empty

    tr = transfers.assign(_key=transfers["nameDest"].astype(str), step_t=transfers["step"])
    co = cashouts.assign(_key=cashouts["nameOrig"].astype(str), step_c=cashouts["step"])
    paired = pd.merge_asof(
        tr,
        co[["_key", "step_c", "nameOrig", "amount"]],
        left_on="step_t",
        right_on="step_c",
        left_by="_key",
        right_by="_key",
        direction="forward",
        allow_exact_matches=False,
        tolerance=int(window_hours),  # `step` is an integer column (hours)
        suffixes=("_t", "_c"),
    )
    matched = paired[
        paired["nameOrig_c"].notna() & (paired["amount_c"] >= 0.9 * paired["amount_t"])
    ]
    if matched.empty:
        return empty

    flagged_keys: set[tuple[int, str, float]] = set()
    for _, row in matched.iterrows():
        flagged_keys.add(
            (int(row["step_t"]), str(row["nameOrig_t"]), round(float(row["amount_t"]), 2))
        )
        flagged_keys.add(
            (int(row["step_c"]), str(row["nameOrig_c"]), round(float(row["amount_c"]), 2))
        )
    d["_key"] = list(zip(d["step"], d["nameOrig"], d["amount"].round(2), strict=True))
    flagged = d[d["_key"].isin(flagged_keys)]
    cols = ["step", "amount", "nameOrig", "nameDest", "category", "merchant"]
    return flagged[cols].assign(reason="balance_drain")


def detect_spend_spikes(df: pd.DataFrame, z_threshold: float = 3.0) -> pd.DataFrame:
    """Flag transactions on days where a spending category's total exceeds its
    trailing baseline by more than `z_threshold` standard deviations."""
    d = df[df["category"].isin(SPENDING_CATEGORIES)].copy()
    if d.empty:
        return pd.DataFrame(columns=["step", "amount", "category", "merchant", "reason"])
    daily = d.groupby([d["date"], d["category"]])["amount"].sum().reset_index(name="total")
    stats = daily.groupby("category")["total"].agg(["mean", "std"]).fillna(0.0)
    daily = daily.merge(stats, on="category", how="left")
    daily["z"] = (daily["total"] - daily["mean"]) / daily["std"].replace(0, np.nan)
    spike_days = daily[daily["z"] >= z_threshold][["date", "category"]]
    flagged = d.merge(spike_days, on=["date", "category"], how="inner")
    return flagged[["step", "date", "amount", "category", "merchant"]].assign(reason="spend_spike")


def detect_recurring_payments(
    df: pd.DataFrame,
    min_occurrences: int = 3,
    max_amount_cv: float = 0.25,
    max_interval_cv: float = 0.35,
) -> pd.DataFrame:
    """Find the focal user's recurring payments: stable amounts at stable intervals."""
    d = df[df["is_focal_user"] & df["type"].isin({"SHOP", "PAYMENT", "SUBSCRIPTION", "TRANSFER"})]
    d = d.sort_values("step")
    out: list[dict[str, Any]] = []
    for merchant, g in d.groupby("merchant", sort=False):
        if len(g) < min_occurrences:
            continue
        amounts = g["amount"].to_numpy(dtype=float)
        mean_amt = float(amounts.mean())
        if mean_amt <= 0:
            continue
        cv_amount = float(amounts.std() / mean_amt)
        gaps = np.diff(g["step"].to_numpy(dtype=float)) / 24.0
        cv_gap = float(gaps.std() / gaps.mean()) if gaps.mean() else 1.0
        if cv_amount <= max_amount_cv and cv_gap <= max_interval_cv:
            out.append(
                {
                    "merchant": merchant,
                    "category": str(g["category"].mode().iloc[0]),
                    "amount": round(mean_amt, 2),
                    "interval_days": round(float(gaps.mean()), 1),
                    "occurrences": len(g),
                    "last_paid": str(g["date"].max()),
                }
            )
    return pd.DataFrame(out)


def rule_risk_flags(df: pd.DataFrame, cfg: dict[str, Any] | None = None) -> pd.DataFrame:
    """Per-transaction rule risk score (0..1) plus a plain-English reason.

    Scores are additive across signatures but capped at 1.0:
        balance drain    -> 1.0
        duplicate charge -> 0.8
        spend spike      -> 0.65
    """
    cfg = cfg or {}
    d = df.copy()
    d["rule_score"] = 0.0
    d["rule_reason"] = ""

    drain = detect_balance_drain(
        d,
        window_hours=float(cfg.get("balance_drain_window_hours", 6.0)),
        min_amount=float(cfg.get("balance_drain_min_amount", 500.0)),
    )
    drain_mask = d["step"].isin(drain["step"])
    d.loc[drain_mask, "rule_score"] = np.maximum(d.loc[drain_mask, "rule_score"], 1.0)
    d.loc[drain_mask, "rule_reason"] = "Balance-draining transfer followed by cash-out"

    dups = detect_duplicate_charges(
        d, window_days=float(cfg.get("duplicate_charge_window_days", 2.0))
    )
    dup_mask = d["step"].isin(dups["step"])
    d.loc[dup_mask, "rule_score"] = np.maximum(d.loc[dup_mask, "rule_score"], 0.8)
    d.loc[dup_mask, "rule_reason"] = "Duplicate charge: same merchant and amount within the window"

    spikes = detect_spend_spikes(d, z_threshold=float(cfg.get("spike_z_threshold", 3.0)))
    spike_mask = d["step"].isin(spikes["step"])
    d.loc[spike_mask, "rule_score"] = np.maximum(d.loc[spike_mask, "rule_score"], 0.65)
    d.loc[spike_mask, "rule_reason"] = "Category spending spike above the usual baseline"

    return d


def compute_financial_health(df: pd.DataFrame) -> dict[str, Any]:
    """A 0-100 financial health score built from explainable components."""
    focal = df[df["is_focal_user"]].copy()
    if focal.empty:
        return {
            "score": 0,
            "components": {},
            "monthly_income": 0.0,
            "monthly_expenses": 0.0,
            "monthly_savings": 0.0,
            "savings_rate": 0.0,
            "buffer_months": 0.0,
        }
    months = int(pd.to_datetime(focal["date"]).dt.to_period("M").nunique())
    income = float(focal.loc[focal["type"].isin({"SALARY", "CASH_IN"}), "amount"].sum())
    savings_out = float(
        focal.loc[(focal["type"] == "TRANSFER") & (focal["category"] == "savings"), "amount"].sum()
    )
    expenses = float(focal.loc[expense_rows(focal), "amount"].sum())
    monthly_income = income / months
    monthly_expenses = expenses / months
    monthly_savings = (income - expenses - savings_out) / months

    savings_rate = monthly_savings / monthly_income if monthly_income > 0 else 0.0
    sub_spend = float(focal.loc[focal["category"] == "subscriptions", "amount"].sum()) / months
    sub_ratio = sub_spend / monthly_income if monthly_income > 0 else 1.0
    monthly_by_cat = focal.loc[expense_rows(focal)].groupby("category")["amount"].sum() / months
    top_share = (
        monthly_by_cat.drop(index=["savings"], errors="ignore").max() / monthly_expenses
        if monthly_expenses > 0 and not monthly_by_cat.empty
        else 1.0
    )
    net_flow = income - expenses - savings_out
    buffer_months = net_flow / monthly_expenses if monthly_expenses > 0 else 0.0

    def clamp(x: float) -> float:
        return float(np.clip(x, 0.0, 1.0))

    components = {
        "savings_rate": round(savings_rate, 3),
        "subscription_ratio": round(sub_ratio, 3),
        "top_category_share": round(float(top_share), 3),
        "buffer_months": round(float(buffer_months), 1),
    }
    score = 100.0 * (
        0.30 * clamp(savings_rate / 0.20)
        + 0.20 * clamp(1.0 - sub_ratio / 0.05)
        + 0.15 * clamp(1.0 - top_share / 0.40)
        + 0.20 * clamp(buffer_months / 6.0)
        + 0.15 * clamp(1.0 - abs(0.5 - savings_rate) * 2.0)
    )
    return {
        "score": int(round(score)),
        "components": components,
        "monthly_income": round(monthly_income, 2),
        "monthly_expenses": round(monthly_expenses, 2),
        "monthly_savings": round(monthly_savings, 2),
        "savings_rate": round(savings_rate, 3),
        "buffer_months": round(float(buffer_months), 1),
    }
