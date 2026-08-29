"""Datagen — Background population ledger generation."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from finance_agent.datagen_pkg.config import _CASH_ACCOUNT, _INFLATION
from finance_agent.datagen_pkg.helpers import _frame
from finance_agent.merchants import sample_merchants, seasonal_multiplier
from finance_agent.personas import (
    DISCRETIONARY_CATEGORIES,
    BackgroundProfile,
    avg_amount_by_category,
    round2,
)


def _background_ledger(
    rng: np.random.Generator,
    profiles: list[BackgroundProfile],
    days: int,
    start: datetime,
    tier: str,
) -> pd.DataFrame:
    """Vectorized-per-account background population (§7)."""
    frames: list[pd.DataFrame] = []
    day_idx = np.arange(days)
    dates = pd.to_datetime(start) + pd.to_timedelta(day_idx, unit="D")
    dom = dates.day.to_numpy()
    month = dates.month.to_numpy()
    years_since = dates.year.to_numpy() - start.year
    infl = np.power(1.0 + _INFLATION, years_since)
    for b in profiles:
        r = rng
        bd = _bg_one(r, b, day_idx, dom, month, infl, days, start)
        frames.append(bd)
        # rare transfer to another background account + cash-out pairs
    if frames:
        out = pd.concat(frames, ignore_index=True)
        out = out[out["amount"] > 0]
        return out
    return _frame(
        step=np.array([], dtype=int),
        type="SHOP",
        amount=np.array([], dtype=float),
        nameOrig="C_BG000000",
        nameDest="M_x",
        merchant="x",
        category="dining",
        subcategory="restaurants",
        transaction_region="R00_portland",
        account_type="background",
    )


def _bg_one(
    r: np.random.Generator,
    b: BackgroundProfile,
    day_idx: np.ndarray,
    dom: np.ndarray,
    month: np.ndarray,
    infl: np.ndarray,
    days: int,
    start: datetime,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    # income
    if b.cadence == "monthly":
        mask = dom == 1
        frames.append(
            _frame(
                step=np.nonzero(mask)[0] * 24 + 8,
                type="SALARY",
                amount=np.full(int(mask.sum()), round2(b.monthly_income)),
                nameOrig=b.pid,
                nameDest=_CASH_ACCOUNT,
                merchant="Payroll Deposit",
                category="income",
                subcategory="payroll",
                transaction_region=b.region,
                account_type="background",
            )
        )
    elif b.income_cash_rate > 0:
        weeks = max(1, int(np.ceil(days / 7)))
        counts = r.poisson(b.income_cash_rate, size=weeks)
        total = int(counts.sum())
        if total > 0:
            ev_week = np.repeat(np.arange(weeks), counts)
            ev_day = np.clip(ev_week * 7 + r.integers(0, 7, total), 0, days - 1)
            amts = np.clip(
                r.lognormal(np.log(max(b.income_cash_mean, 1.0)), 0.7, total), 20.0, 15000.0
            )
            frames.append(
                _frame(
                    step=ev_day * 24 + r.integers(9, 13),
                    type="CASH_IN",
                    amount=np.round(amts, 2),
                    nameOrig=b.pid,
                    nameDest=_CASH_ACCOUNT,
                    merchant="GigPay",
                    category="income",
                    subcategory="freelance",
                    transaction_region=b.region,
                    account_type="background",
                )
            )
    # spending — batched draws (no per-item r.choice / r.lognormal / catalog
    # scan; the bench tier runs this once per background profile, ~20k times)
    season_groc = np.array([seasonal_multiplier("groceries", m) for m in month])
    rates = b.spend_rate * season_groc
    counts = r.poisson(rates)
    n = int(counts.sum())
    if n > 0:
        ev_day = np.repeat(day_idx, counts)
        cat_pool = np.asarray(DISCRETIONARY_CATEGORIES, dtype=object)
        cat_pos = r.integers(0, len(cat_pool), n)
        cats = cat_pool[cat_pos]
        means = np.asarray(
            [avg_amount_by_category(c, 1.0) for c in DISCRETIONARY_CATEGORIES], dtype=float
        )[cat_pos]
        # Explicit float64: numpy-stubs type np.round(..., 2) as _16Bit, which
        # would clash with the income-branch float64 `amts` assignment above.
        amts = np.round(np.clip(r.lognormal(np.log(means), 0.6, size=n), 3.0, 2000.0), 2).astype(
            np.float64
        )
        merchants = np.empty(n, dtype=object)
        subcats = np.empty(n, dtype=object)
        for ci, c in enumerate(DISCRETIONARY_CATEGORIES):
            sel = np.nonzero(cat_pos == ci)[0]
            if sel.size:
                ms = sample_merchants(r, str(c), int(sel.size))
                names = np.asarray([m["name"] for m in ms], dtype=object)
                merchants[sel] = names
                subcats[sel] = np.asarray([m["subcategory"] for m in ms], dtype=object)
        frames.append(
            _frame(
                step=ev_day * 24 + r.integers(8, 23, n),
                type="SHOP",
                amount=amts,
                nameOrig=b.pid,
                nameDest=np.asarray(["M_" + str(m) for m in merchants], dtype=object),
                merchant=merchants,
                category=cats,
                subcategory=subcats,
                transaction_region=b.region,
                account_type="background",
            )
        )
    # ATM cash-outs
    co_counts = r.poisson(0.6, size=max(1, days // 28))
    if int(co_counts.sum()) > 0:
        ev_month = np.repeat(np.arange(max(1, days // 28)), co_counts)
        ev_day = np.clip(ev_month * 28 + r.integers(5, 26, int(co_counts.sum())), 0, days - 1)
        frames.append(
            _frame(
                step=ev_day * 24 + r.integers(9, 21),
                type="CASH_OUT",
                amount=np.round(r.uniform(40.0, 300.0, int(co_counts.sum())), 2),
                nameOrig=b.pid,
                nameDest=_CASH_ACCOUNT,
                merchant="ATM Withdrawal",
                category="transfer",
                subcategory="atm",
                transaction_region=b.region,
                account_type="background",
            )
        )
    # bust-out fraud for a fraction of accounts
    if b.bust_out and days >= fp._MIN_DAYS["bust_out"]:
        ctx = fp.PatternCtx(
            rng=r,
            persona=_bust_persona(b),
            days=days,
            start=start,
            bg_pool=[],
            day_lo=max(1, int(days * 0.6)),
            day_hi=days,
        )
        frames.append(pd.DataFrame(fp.inject_background_patterns(ctx, bust=True)))
    if frames:
        return pd.concat(frames, ignore_index=True)
    return _frame(
        step=np.array([], dtype=int),
        type="SHOP",
        amount=np.array([], dtype=float),
        nameOrig=b.pid,
        nameDest="M_x",
        merchant="x",
        category="dining",
        subcategory="restaurants",
        transaction_region=b.region,
        account_type="background",
    )
