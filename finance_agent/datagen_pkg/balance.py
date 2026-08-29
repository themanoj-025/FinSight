"""Datagen — Balance resolution (clamped cumulative sum, drain resolution)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from finance_agent.constants import CREDIT_TYPES
from finance_agent.personas import BackgroundProfile, Persona, round2


def _bust_persona(b: BackgroundProfile) -> Persona:
    """A minimal persona facade so bust-out rows carry the bg account's id."""
    return Persona(
        pid=b.pid,
        archetype=b.archetype,
        annual_income_start=b.monthly_income * 12,
        income_cadence=b.cadence,
        payday_dom=1,
        payday_weekday=None,
        rent_monthly=0.0,
        savings_rate=0.0,
        category_weights=dict(b.cat_weights),
        subscriptions=[],
        col_multiplier=1.0,
        spend_multiplier=1.0,
        velocity=0.0,
        has_credit=False,
        credit_share=0.0,
        cash_in_rate=0.0,
        cash_in_mean=0.0,
        loan_monthly=0.0,
        home_region=b.region,
        travel_regions=list(b.travel_regions),
        raises=[],
        opening_balance=b.opening,
        monthly_fixed=0.0,
        monthly_discretionary=0.0,
        p2p_share=0.0,
        accounts=[b.pid],
    )


# ------------------------------------------------------------------ balances
def _balance_one(
    df: pd.DataFrame,
    openings: dict[str, float],
    credit_accounts: set[str],
    tracked: set[str],
) -> pd.DataFrame:
    """Fill old/new balances with a per-account clamped cumulative sum.

    Lindley's recursion — balance_t = T_t - min(0, min_prefix T) — gives the
    clamped running balance in O(events) vectorized per account, no per-row
    loop. Events are sorted once by (account, step) with a single stable
    lexsort, so the whole pass is O(n log n) instead of the old O(accounts x
    events) full-array scan per account — the bench tier has ~20k accounts,
    where per-account scans dominated. Debits that would overdraw are clamped
    to the available balance and the row's ``amount`` is reduced to match.
    Credit accounts clamp at 0 from above (balance = negative outstanding).
    """
    n = len(df)
    df = df.reset_index(drop=True)
    for c in ("oldbalanceOrg", "newbalanceOrig", "oldbalanceDest", "newbalanceDest"):
        if c not in df.columns:
            df[c] = 0.0
    is_credit_type = df["type"].isin(CREDIT_TYPES).to_numpy()
    amt = df["amount"].to_numpy(dtype=float)
    is_transfer = (df["type"] == "TRANSFER").to_numpy()
    dst_mask = is_transfer & df["nameDest"].isin(tracked).to_numpy()

    src_acct = df["nameOrig"].to_numpy(dtype=object)
    src_step = df["step"].to_numpy(dtype=np.int64)
    ev_acct = np.concatenate([src_acct, df["nameDest"].to_numpy(dtype=object)[dst_mask]])
    ev_step = np.concatenate([src_step, df["step"].to_numpy(dtype=np.int64)[dst_mask]])
    ev_row = np.concatenate([np.arange(n), np.nonzero(dst_mask)[0]])
    ev_is_dest = np.concatenate([np.zeros(n, dtype=bool), np.ones(int(dst_mask.sum()), dtype=bool)])
    ev_src_is_credit = np.concatenate([is_credit_type, np.zeros(int(dst_mask.sum()), dtype=bool)])

    old_bal = np.full(len(ev_acct), 0.0)
    new_bal = np.full(len(ev_acct), 0.0)
    applied = np.empty(len(ev_acct), dtype=float)
    applied[:] = np.concatenate([amt, amt[dst_mask]])

    # One stable sort by (account, step). np.lexsort is stable, so equal-step
    # ties keep append order (src before dst) — exactly what the legacy
    # per-account stable argsort produced. Groups are contiguous slices, so no
    # per-account scan of the whole event array is needed.
    order = np.lexsort((ev_step, ev_acct))
    sorted_acct = ev_acct[order]
    sorted_is_src_credit = ev_src_is_credit[order]
    sorted_is_dest = ev_is_dest[order]
    sorted_applied = applied[order]
    bounds = np.flatnonzero(np.concatenate([[True], sorted_acct[1:] != sorted_acct[:-1], [True]]))

    for g in range(len(bounds) - 1):
        lo, hi = bounds[g], bounds[g + 1]
        sel = order[lo:hi]  # global event positions (fancy-indexed writes must
        # target `sel` directly — `arr[mask][order] = v` would write to a copy)
        acct = str(sorted_acct[lo])
        opening = float(openings.get(acct, 0.0))
        clamp_hi = acct in credit_accounts
        is_src_credit_sel = sorted_is_src_credit[lo:hi]
        applied_sel = sorted_applied[lo:hi]
        deltas0 = np.where(is_src_credit_sel, applied_sel, -applied_sel)
        # iteration 1: preliminary balances to detect clamped debits
        bal = _clamped_cumsum(deltas0, opening, clamp_hi)
        before = np.concatenate([[opening], bal[:-1]])
        # effective amounts: source debits clamp to available balance
        is_src_debit = ~sorted_is_dest[lo:hi]
        eff = applied_sel.copy()
        avail = before if not clamp_hi else -before
        eff = np.where(
            is_src_debit & (deltas0 < 0) & (applied_sel > avail), np.maximum(avail, 0.0), eff
        )
        deltas1 = np.where(is_src_credit_sel, eff, -eff)
        # iteration 2: exact clamped cumsum with effective amounts. Balances are
        # rounded to cents so the emitted CSV/store/pandas paths agree exactly
        # (the legacy per-row Account code kept balances at 2dp too).
        bal2 = _clamped_cumsum(deltas1, opening, clamp_hi)
        before2 = np.concatenate([[opening], bal2[:-1]])
        old_bal[sel] = np.round(before2, 2)
        new_bal[sel] = np.round(bal2, 2)
        applied[sel] = np.round(np.maximum(eff, 0.0), 2)

    # scatter back by row position (df index was reset to 0..n-1, so label and
    # positional lookup coincide; label-column assignment is used because some
    # pandas builds refuse get_loc on freshly-added str columns)
    src_ev = ~ev_is_dest
    dst_ev = ev_is_dest
    src_pos = ev_row[src_ev]
    dst_pos = ev_row[dst_ev]
    # NB: the canonical column is `oldbalanceOrg` (PaySim legacy name) — the
    # balance pass must write there, never to a `oldbalanceOrig` lookalike.
    df.loc[src_pos, "oldbalanceOrg"] = old_bal[src_ev]
    df.loc[src_pos, "newbalanceOrig"] = new_bal[src_ev]
    df.loc[src_pos, "amount"] = applied[src_ev]
    df.loc[dst_pos, "oldbalanceDest"] = old_bal[dst_ev]
    df.loc[dst_pos, "newbalanceDest"] = new_bal[dst_ev]
    return df


def _clamped_cumsum(delta: np.ndarray, opening: float, clamp_hi: bool) -> np.ndarray:
    """Clamped running balance (Lindley): lower-bound 0, or upper-bound 0."""
    t = np.concatenate([[opening], opening + np.cumsum(delta)])
    if clamp_hi:
        return (t - np.maximum.accumulate(np.maximum(t, 0.0)))[1:]
    return (t - np.minimum.accumulate(np.minimum(t, 0.0)))[1:]


def _resolve_drains(
    df: pd.DataFrame, openings: dict[str, float], credit_accounts: set[str], tracked: set[str]
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Two-pass balance resolution for balance-dependent fraud amounts.

    Pass 1 zeroes the drain rows, reads each drain's available balance, sizes
    the transfer (``_drain_ratio`` of it), bumps victim openings, links paired
    cash-outs, then pass 2 recomputes exact balances.
    """
    df = df.reset_index(drop=True)
    has_drains = "_drain_ratio" in df.columns
    if not has_drains or not df["_drain_ratio"].notna().any():
        return _balance_one(df, openings, credit_accounts, tracked), openings

    df1 = df.copy()
    drain_rows = df1["_drain_ratio"].notna().to_numpy()
    df1.loc[drain_rows, "amount"] = 0.0
    df1 = _balance_one(df1, openings, credit_accounts, tracked)
    drain_idx = np.nonzero(drain_rows)[0]
    for i in drain_idx:
        ratio = float(df1["_drain_ratio"].iloc[i])
        amt = ratio * max(0.0, float(df1["oldbalanceOrg"].iloc[i]))
        df1.loc[i, "amount"] = round2(amt)
        victim = df1["_victim_id"].iloc[i]
        if victim and str(victim) != "":
            openings[str(victim)] = max(openings.get(str(victim), 0.0), round2(amt * 1.2))
    pair_ids = df1["_pair_id"].notna().to_numpy()
    if pair_ids.any():
        # Vectorized pair sizing: a valid pair has exactly one non-CASH_OUT
        # row; its amount sizes every CASH_OUT row of the pair. (Replaces the
        # old per-pair `df1[_pair_id == pid]` scan, which was O(pairs x rows).)
        sub = df1.loc[pair_ids]
        non_co = sub.loc[sub["type"] != "CASH_OUT"]
        per_pair = non_co.groupby("_pair_id")["amount"].agg(["count", "first"])
        per_pair = per_pair[per_pair["count"] == 1]
        amt_by_pair = per_pair["first"].round(2).to_dict()
        mapped = df1["_pair_id"].map(amt_by_pair)
        cashout_rows = (df1["type"] == "CASH_OUT") & mapped.notna()
        df1.loc[cashout_rows, "amount"] = mapped[cashout_rows].to_numpy()
    df = df1.drop(
        columns=[c for c in ("_drain_ratio", "_pair_id", "_victim_id") if c in df1.columns]
    )
    return _balance_one(df, openings, credit_accounts, tracked), openings
