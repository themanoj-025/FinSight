"""Unit tests for finance_agent.rules — crafted edge cases, no fixtures needed."""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from finance_agent import rules

BASE_TS = datetime(2025, 1, 1)

REQUIRED = [
    "step",
    "type",
    "amount",
    "nameOrig",
    "oldbalanceOrg",
    "newbalanceOrig",
    "nameDest",
    "oldbalanceDest",
    "newbalanceDest",
    "merchant",
    "category",
    "datetime",
    "date",
    "is_focal_user",
    "isFraud",
    "isFlaggedFraud",
    "is_anomaly",
    "anomaly_type",
]


def make_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col in REQUIRED:
        if col not in df.columns:
            df[col] = (
                "" if col in ("merchant", "category", "anomaly_type", "datetime", "date") else 0
            )
    df["date"] = df["date"].astype(str)
    return df


def txn(
    step, txn_type, amount, merchant, category, focal=True, orig="C_A", dest="C_B", old=0.0, new=0.0
) -> dict:
    ts = BASE_TS + timedelta(hours=int(step))
    return {
        "step": step,
        "type": txn_type,
        "amount": amount,
        "nameOrig": orig,
        "oldbalanceOrg": old,
        "newbalanceOrig": new,
        "nameDest": dest,
        "merchant": merchant,
        "category": category,
        "datetime": ts.isoformat(timespec="minutes"),
        "date": ts.date().isoformat(),
        "is_focal_user": focal,
    }


# ------------------------------------------------------------------ balance drain
def test_balance_drain_detected_when_balance_hits_exactly_zero() -> None:
    df = make_df(
        [
            txn(100, "TRANSFER", 2000.0, "WireOut", "transfer", old=2000.0, new=0.0),
            txn(102, "CASH_OUT", 2000.0, "ATM Withdrawal", "transfer", orig="C_B"),
        ]
    )
    flagged = rules.detect_balance_drain(df)
    assert len(flagged) == 2
    assert (flagged["reason"] == "balance_drain").all()


def test_balance_drain_not_flagged_for_healthy_transfer() -> None:
    df = make_df(
        [
            txn(100, "TRANSFER", 500.0, "Peer Transfer", "transfer", old=2500.0, new=2000.0),
            txn(102, "CASH_OUT", 500.0, "ATM Withdrawal", "transfer", orig="C_B"),
        ]
    )
    assert len(rules.detect_balance_drain(df)) == 0


def test_balance_drain_requires_cashout_within_window() -> None:
    df = make_df(
        [
            txn(100, "TRANSFER", 2000.0, "WireOut", "transfer", old=2000.0, new=0.0),
            txn(
                500, "CASH_OUT", 2000.0, "ATM Withdrawal", "transfer", orig="C_B"
            ),  # way outside 6h
        ]
    )
    assert len(rules.detect_balance_drain(df)) == 0


# ------------------------------------------------------------------ duplicates
def test_duplicate_charge_flagged_within_window() -> None:
    df = make_df(
        [
            txn(10, "SHOP", 59.99, "SteamGames", "entertainment"),
            txn(12, "SHOP", 59.99, "SteamGames", "entertainment"),
        ]
    )
    dup = rules.detect_duplicate_charges(df)
    assert len(dup) == 1
    assert dup["merchant"].iloc[0] == "SteamGames"


def test_duplicate_charge_outside_window_not_flagged() -> None:
    df = make_df(
        [
            txn(10, "SHOP", 59.99, "SteamGames", "entertainment"),
            txn(200, "SHOP", 59.99, "SteamGames", "entertainment"),  # 190h later
        ]
    )
    assert len(rules.detect_duplicate_charges(df)) == 0


def test_duplicate_charge_ignores_transfers_and_other_users() -> None:
    df = make_df(
        [
            txn(10, "SHOP", 59.99, "SteamGames", "entertainment", focal=False),
            txn(12, "SHOP", 59.99, "SteamGames", "entertainment", focal=False),
            txn(20, "TRANSFER", 59.99, "Peer Transfer", "transfer", focal=True),
            txn(22, "TRANSFER", 59.99, "Peer Transfer", "transfer", focal=True),
        ]
    )
    assert len(rules.detect_duplicate_charges(df)) == 0


# ------------------------------------------------------------------ spend spikes
def test_spend_spike_flagged_over_baseline() -> None:
    rows = [txn(h * 24 + 12, "SHOP", 30.0, "Bistro", "dining") for h in range(20)]
    rows += [txn(20 * 24 + 12 + i, "SHOP", 300.0, "Bistro", "dining") for i in range(1)]
    df = make_df(rows)
    spikes = rules.detect_spend_spikes(df, z_threshold=3.0)
    assert len(spikes) >= 1
    assert (spikes["category"] == "dining").all()


def test_normal_spending_has_no_spike() -> None:
    rows = [txn(h * 24 + 12, "SHOP", float(25 + (h % 7)), "Bistro", "dining") for h in range(30)]
    assert len(rules.detect_spend_spikes(make_df(rows), z_threshold=3.0)) == 0


# ------------------------------------------------------------------ recurring
def test_recurring_payment_detected_with_noisy_intervals() -> None:
    rows = [
        txn(24 * 30, "SUBSCRIPTION", 15.49, "Netflix", "subscriptions"),
        txn(24 * 59, "SUBSCRIPTION", 15.49, "Netflix", "subscriptions"),  # 29 days
        txn(24 * 90, "SUBSCRIPTION", 15.49, "Netflix", "subscriptions"),  # 31 days
        txn(24 * 121, "SUBSCRIPTION", 15.49, "Netflix", "subscriptions"),
    ]
    rec = rules.detect_recurring_payments(make_df(rows))
    assert not rec.empty
    assert rec["merchant"].iloc[0] == "Netflix"
    assert 28 <= rec["interval_days"].iloc[0] <= 32


def test_unstable_amounts_not_recurring() -> None:
    rows = [
        txn(24 * 30, "SHOP", 10.0, "WeirdStore", "shopping"),
        txn(24 * 60, "SHOP", 60.0, "WeirdStore", "shopping"),
        txn(24 * 90, "SHOP", 110.0, "WeirdStore", "shopping"),
    ]
    assert rules.detect_recurring_payments(make_df(rows)).empty


# ------------------------------------------------------------------ health & flags
def test_financial_health_shape_and_bounds() -> None:
    rows = []
    for month in (0, 1, 2):
        rows.append(txn(month * 24 * 30 + 8, "SALARY", 5400.0, "Acme Corp Payroll", "income"))
        rows.append(txn(month * 24 * 30 + 20, "TRANSFER", 1650.0, "RentCo", "housing"))
        for d in range(1, 10):
            rows.append(txn(month * 24 * 30 + d * 24, "SHOP", 120.0, "Grocer", "groceries"))
    health = rules.compute_financial_health(make_df(rows))
    assert 0 <= health["score"] <= 100
    assert set(health["components"]) == {
        "savings_rate",
        "subscription_ratio",
        "top_category_share",
        "buffer_months",
    }
    assert health["monthly_income"] == pytest.approx(5400.0)


def test_rule_risk_flags_scores_and_reasons() -> None:
    df = make_df(
        [
            txn(100, "TRANSFER", 2000.0, "WireOut", "transfer", old=2000.0, new=0.0),
            txn(102, "CASH_OUT", 2000.0, "ATM Withdrawal", "transfer", orig="C_B"),
            txn(200, "SHOP", 59.99, "SteamGames", "entertainment"),
            txn(201, "SHOP", 59.99, "SteamGames", "entertainment"),
            txn(300, "SHOP", 25.0, "Coffee", "dining"),
        ]
    )
    flagged = rules.rule_risk_flags(df, {})
    drain_rows = flagged[flagged["rule_score"] == 1.0]
    dup_rows = flagged[flagged["rule_score"] == 0.8]
    assert len(drain_rows) == 2
    assert len(dup_rows) == 1
    assert drain_rows["rule_reason"].iloc[0] != ""
    assert (flagged["rule_score"].between(0, 1)).all()


@pytest.mark.slow
def test_rule_risk_flags_100k_rows_under_budget() -> None:
    """2.7: vectorized detectors must stay fast at scale (excluded from CI fast job)."""
    import time

    rng = np.random.default_rng(0)
    n = 100_000
    cats = [
        "groceries",
        "dining",
        "transport",
        "utilities",
        "entertainment",
        "shopping",
        "health",
        "subscriptions",
    ]
    types = ["TRANSFER", "CASH_OUT", "SHOP", "PAYMENT", "SALARY", "CASH_IN", "DEBIT"]
    df = make_df(
        [
            {
                "step": int(s),
                "type": str(t),
                "amount": round(float(a), 2),
                "nameOrig": str(o),
                "oldbalanceOrg": round(float(b), 2),
                "newbalanceOrig": 0.0,
                "nameDest": str(d),
                "merchant": "m",
                "category": str(c),
                "is_focal_user": bool(f),
            }
            for s, t, a, o, b, d, c, f in zip(
                rng.integers(0, 100_000, n),
                rng.choice(types, n),
                rng.uniform(1, 5000, n),
                rng.choice([f"C{i:05d}" for i in range(2000)], n),
                rng.uniform(0, 10000, n),
                rng.choice([f"D{i:05d}" for i in range(2000)], n),
                rng.choice(cats, n),
                rng.choice([True, False], n),
                strict=True,
            )
        ]
    )
    t0 = time.perf_counter()
    out = rules.rule_risk_flags(df, {})
    elapsed = time.perf_counter() - t0
    assert len(out) == n
    assert elapsed < 5.0, f"rule_risk_flags took {elapsed:.2f}s on 100k rows"
