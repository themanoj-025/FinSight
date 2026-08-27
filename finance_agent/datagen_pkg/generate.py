"""Datagen — Main entry points (generate_dataset, persona_manifest, tier_stats)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from finance_agent.datagen_pkg.background import _background_ledger
from finance_agent.datagen_pkg.balance import _resolve_drains
from finance_agent.datagen_pkg.config import (
    FINAL_COLUMNS,
    TIER_DEFAULTS,
    MERCHANTS_INDEX,
)
from finance_agent.datagen_pkg.helpers import focal_user_ids
from finance_agent.datagen_pkg.persona_ledger import _persona_ledger
from finance_agent.merchants import CATEGORY_GROUP
from finance_agent.personas import avg_amount_by_category, round2, sample_background, sample_personas

def generate_dataset(
    *,
    days: int,
    seed: int,
    tier: str = "tiny",
    users: list[str] | None = None,
    n_background_accounts: int | None = None,
    start_date: str = "2025-01-01",
    n_fraud_pairs: int = 20,
    verbose: bool = False,
) -> pd.DataFrame:
    """Generate the full synthetic ledger for a tier (see module docstring)."""
    if tier not in TIER_DEFAULTS:
        raise ValueError(f"unknown tier {tier!r} (expected tiny|demo|bench)")
    td = TIER_DEFAULTS[tier]
    n_bg = n_background_accounts if n_background_accounts is not None else int(td["background"])
    scale = float(td["fraud_scale"])
    bust_fraction = float(td["bust_fraction"])

    users = list(users) if users else focal_user_ids(1)
    start = datetime.fromisoformat(start_date)

    ss = np.random.SeedSequence(seed)
    batch_rng = np.random.default_rng(ss.spawn(1)[0])
    personas = sample_personas(
        users, int(ss.spawn(2)[0].generate_state(1)[0] & 0xFFFFFFFF), batch_rng
    )
    for p in personas:  # deterministic employer per persona
        idx = [
            i
            for i, m in enumerate(MERCHANTS_INDEX)
            if m["category"] == "income" and m["subcategory"] == "payroll"
        ]
        p.employer = str(MERCHANTS_INDEX[int(batch_rng.choice(idx))]["name"])

    bg_profiles = sample_background(
        n_bg,
        int(ss.spawn(3)[0].generate_state(1)[0] & 0xFFFFFFFF),
        np.random.default_rng(ss.spawn(4)[0]),
        bust_fraction=bust_fraction,
    )
    bg_pool = [b.pid for b in bg_profiles]

    frames: list[pd.DataFrame] = []
    openings: dict[str, float] = {}
    persona_meta: dict[str, dict[str, Any]] = {}

    for i, p in enumerate(personas):
        rng = np.random.default_rng(ss.spawn(len(personas) * 2)[i])
        frames.append(_persona_ledger(rng, p, days, start, bg_pool, scale))
        openings[p.pid] = p.opening_balance
        openings[f"{p.pid}_Sav"] = round2(max(0.0, p.opening_balance * 0.1))
        if p.has_credit:
            openings[f"{p.pid}_Cred"] = 0.0
        persona_meta[p.pid] = {
            "id": p.pid,
            "archetype": p.archetype,
            "annual_income_start": p.annual_income_start,
            "home_region": p.home_region,
            "accounts": list(p.accounts),
        }

    bg_rng = np.random.default_rng(ss.spawn(len(personas) * 2 + 1)[0])
    bg_ledger = _background_ledger(bg_rng, bg_profiles, days, start, tier)
    frames.append(bg_ledger)
    for b in bg_profiles:
        openings[b.pid] = b.opening

    df = pd.concat(frames, ignore_index=True)
    # Keep placeholder rows (amount 0.0) that carry drain/pair markers — the
    # balance pass sizes them; everything else must be a real positive amount.
    keep = df["amount"].to_numpy() > 0
    for marker in ("_drain_ratio", "_pair_id"):
        if marker in df.columns:
            keep |= df[marker].notna().to_numpy()
    df = df[keep].copy()

    # tracked destination accounts (dest balances maintained)
    tracked = set(openings)
    credit_accounts = {f"{p.pid}_Cred" for p in personas if p.has_credit}
    df, openings = _resolve_drains(df, openings, credit_accounts, tracked)
    df = df[df["amount"] > 0].reset_index(drop=True)

    # ---- derived columns ---------------------------------------------------
    df["datetime"] = (pd.to_datetime(start) + pd.to_timedelta(df["step"], unit="h")).dt.strftime(
        "%Y-%m-%dT%H:%M"
    )
    df["date"] = df["datetime"].str[:10]
    df["category_group"] = df["category"].map(CATEGORY_GROUP).fillna("other")
    df["isFlaggedFraud"] = (df["amount"] > 200_000).astype(int)
    df["is_focal_user"] = df["nameOrig"].isin(set(users))
    focal_ids = set(users)
    acct_to_pid: dict[str, str] = {}
    for p in personas:
        for acct in p.accounts:
            acct_to_pid[acct] = p.pid
    for b in bg_profiles:
        acct_to_pid[b.pid] = b.pid
    df["persona_id"] = df["nameOrig"].map(acct_to_pid).fillna(df["nameOrig"])
    df["is_focal_user"] = df["nameOrig"].isin(focal_ids)
    archetype_by_pid = {p.pid: p.archetype for p in personas}
    archetype_by_pid.update({b.pid: b.archetype for b in bg_profiles})
    df["persona_archetype"] = df["persona_id"].map(archetype_by_pid).fillna("background")
    region_by_pid = {p.pid: p.home_region for p in personas}
    region_by_pid.update({b.pid: b.region for b in bg_profiles})
    df["home_region"] = df["persona_id"].map(region_by_pid).fillna("R00_portland")
    df["merchant_region"] = df["transaction_region"]
    df["simulation_year"] = pd.to_datetime(df["date"]).dt.year

    for c in ("anomaly_type", "fraud_archetype", "subcategory"):
        df[c] = df[c].fillna("").astype(str)

    # label realism: discovery lag on ~2% of fraud rows
    lag_rng = np.random.default_rng(ss.spawn(5)[0])
    fraud = df["isFraud"].to_numpy() == 1
    lag_mask = fraud & (lag_rng.random(len(df)) < 0.02)
    lag_hours = lag_rng.integers(24, 30 * 24, size=int(lag_mask.sum()))
    reported = df["step"].to_numpy().copy()
    reported[lag_mask] = reported[lag_mask] + lag_hours
    df["label_reported_at_step"] = reported

    df = df.sort_values("step", kind="stable").reset_index(drop=True)
    df = df[FINAL_COLUMNS].astype({"amount": float})
    for c in ("isFraud", "is_anomaly", "isFlaggedFraud"):
        df[c] = df[c].astype(int)
    df["is_focal_user"] = df["is_focal_user"].astype(bool)
    return df


def persona_manifest(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-persona summary — used for the CLI manifest JSON + docs."""
    rows: list[dict[str, Any]] = []
    for pid, g in df[df["is_focal_user"]].groupby("persona_id"):
        rows.append(
            {
                "id": str(pid),
                "archetype": str(g["persona_archetype"].iloc[0]),
                "transactions": int(len(g)),
                "fraud_count": int((g["isFraud"] == 1).sum()),
                "home_region": str(g["home_region"].iloc[0]),
            }
        )
    return rows


def tier_stats(df: pd.DataFrame, tier: str) -> dict[str, Any]:
    """Machine-readable stats printed by the CLI (rows, fraud rate, patterns)."""
    fraud = df["isFraud"].to_numpy()
    pat_counts = (
        df.loc[df["isFraud"] == 1, "fraud_archetype"].value_counts().to_dict()
        if "fraud_archetype" in df
        else {}
    )
    return {
        "tier": tier,
        "rows": int(len(df)),
        "fraud": int(fraud.sum()),
        "fraud_rate": round(float(fraud.mean()), 6),
        "anomalies": int(df["is_anomaly"].sum()),
        "focal_transactions": int(df["is_focal_user"].sum()),
        "personas": int(df["persona_id"].nunique()) if "persona_id" in df else 0,
        "fraud_archetypes": {str(k): int(v) for k, v in sorted(pat_counts.items())},
        "columns": list(df.columns),
    }
