"""Datagen — Transaction frame generators (savings, salary, spend, credit, trips, patterns)."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from finance_agent import fraud_patterns as fp
from finance_agent.datagen_pkg.config import _CASH_ACCOUNT
from finance_agent.datagen_pkg.helpers import _CLUSTER, _days_since_payday, _frame
from finance_agent.merchants import (
    sample_merchants,
    seasonal_multiplier,
)
from finance_agent.personas import (
    DISCRETIONARY_CATEGORIES,
    Persona,
    amount_sigma_by_category,
    avg_amount_by_category,
    round2,
)


def _savings_transfer(p: Persona, steps: np.ndarray, amts: np.ndarray) -> pd.DataFrame:
    if steps.size == 0:
        return _frame(
            step=np.array([], dtype=int),
            type="TRANSFER",
            amount=np.array([], dtype=float),
            nameOrig=p.pid,
            nameDest=p.pid + "_Sav",
            merchant="AutoSavings",
            category="savings",
            subcategory="auto_transfer",
            transaction_region=p.home_region,
            account_type="checking",
        )
    df = _frame(
        step=steps,
        type="TRANSFER",
        amount=np.round(np.clip(amts, 0.0, None), 2),
        nameOrig=p.pid,
        nameDest=p.pid + "_Sav",
        merchant="AutoSavings",
        category="savings",
        subcategory="auto_transfer",
        transaction_region=p.home_region,
        account_type="checking",
    )
    sav = _frame(
        step=steps,
        type="CASH_IN",
        amount=np.round(np.clip(amts, 0.0, None), 2),
        nameOrig=p.pid + "_Sav",
        nameDest=_CASH_ACCOUNT,
        merchant="AutoSavings",
        category="savings",
        subcategory="auto_transfer",
        transaction_region=p.home_region,
        account_type="savings",
    )
    return pd.concat([df, sav], ignore_index=True)


def _salaried_cashin(
    rng: np.random.Generator,
    p: Persona,
    days: int,
    dom: np.ndarray,
    month: np.ndarray,
    start: datetime,
    raise_mult: np.ndarray,
) -> pd.DataFrame:
    """Monthly freelance deposits for salaried personas (guaranteed >= 1)."""
    months = np.unique(month)
    prob = min(1.0, p.cash_in_rate)
    fire = rng.random(len(months)) < prob
    if days >= 60 and not fire.any():
        fire[rng.integers(0, len(months))] = True
    sel = months[fire]
    if sel.size == 0:
        return _frame(
            step=np.array([], dtype=int),
            type="CASH_IN",
            amount=np.array([], dtype=float),
            nameOrig=p.pid,
            nameDest=_CASH_ACCOUNT,
            merchant="Freelance Income",
            category="income",
            subcategory="freelance",
            transaction_region=p.home_region,
            account_type="checking",
        )
    doms = rng.integers(18, 26, sel.size)
    steps = []
    for i, m in enumerate(sel):
        day = doms[i]
        candidates = np.nonzero((month == m) & (dom >= day))[0]
        first = candidates[0] if candidates.size else int(np.nonzero(month == m)[0][0])
        steps.append(int(first) * 24 + 12)
    amts = np.clip(rng.lognormal(np.log(max(p.cash_in_mean, 50.0)), 0.5, sel.size), 50.0, 3000.0)
    return _frame(
        step=np.asarray(steps, dtype=int),
        type="CASH_IN",
        amount=np.round(amts, 2),
        nameOrig=p.pid,
        nameDest=_CASH_ACCOUNT,
        merchant="Freelance Income",
        category="income",
        subcategory="freelance",
        transaction_region=p.home_region,
        account_type="checking",
    )


def _spend_frame(
    p: Persona,
    steps: np.ndarray,
    amts: np.ndarray,
    txn_type: str,
    dest: str,
    category: str,
    subcategory: str,
    merchant: str,
    dest_is_tracked: bool = False,
) -> pd.DataFrame:
    if steps.size == 0:
        return _frame(
            step=np.array([], dtype=int),
            type=txn_type,
            amount=np.array([], dtype=float),
            nameOrig=p.pid,
            nameDest=dest,
            merchant=merchant,
            category=category,
            subcategory=subcategory,
            transaction_region=p.home_region,
            account_type="checking",
        )
    return _frame(
        step=steps,
        type=txn_type,
        amount=np.round(np.clip(amts, 0.0, None), 2),
        nameOrig=p.pid,
        nameDest=dest,
        merchant=merchant,
        category=category,
        subcategory=subcategory,
        transaction_region=p.home_region,
        account_type="checking",
    )


def _discretionary(
    rng: np.random.Generator,
    p: Persona,
    days: int,
    dom: np.ndarray,
    month: np.ndarray,
    payday_days: np.ndarray,
    day_idx: np.ndarray,
) -> pd.DataFrame:
    """All discretionary category spend for a persona, vectorized per category."""
    frames: list[pd.DataFrame] = []
    ds = _days_since_payday(payday_days, day_idx)
    cluster = np.asarray([_CLUSTER[int(d)] for d in ds], dtype=float)
    # budget-consistency scale across categories (deterministic, pre-draw)
    expected: dict[str, float] = {}
    for cat in DISCRETIONARY_CATEGORIES:
        season = np.array([seasonal_multiplier(cat, m) for m in month])
        rates = p.velocity * p.category_weights[cat] * season * cluster * p.spend_multiplier
        expected[cat] = rates.sum() * avg_amount_by_category(cat, p.col_multiplier)
    window_budget = p.monthly_discretionary * (days / 30.44)
    total_expected = max(sum(expected.values()), 1.0)
    scale = float(np.clip(window_budget / total_expected, 0.5, 1.6))

    for cat in DISCRETIONARY_CATEGORIES:
        season = np.array([seasonal_multiplier(cat, m) for m in month])
        rates = p.velocity * p.category_weights[cat] * season * cluster * p.spend_multiplier
        counts = rng.poisson(rates)
        n = int(counts.sum())
        if n == 0:
            continue
        ev_day = np.repeat(day_idx, counts)
        mean = avg_amount_by_category(cat, p.col_multiplier) * scale
        sigma = amount_sigma_by_category(cat)
        amts = np.clip(rng.lognormal(np.log(mean), sigma, n), mean * 0.2, mean * 6.0)
        hours = rng.integers(8, 23, n)
        merchants = sample_merchants(rng, cat, n)
        # 96% home region, rest a usual travel region
        travel_region = np.asarray(
            [p.travel_regions[0]] * n if p.travel_regions else [p.home_region] * n
        )
        region = np.where(rng.random(n) < 0.96, p.home_region, travel_region)
        # Plain np.ndarray annotations: np.full is stub-typed 1-D while
        # np.where below is generic-shape (ndarray[tuple[int, ...]]) — pinning
        # either shape on the variable breaks the other on some numpy stub
        # versions (numpy 2.2.6 vs 2.4+ differ), so keep both unparameterized.
        acc: np.ndarray = np.full(n, "checking", dtype=object)
        orig: np.ndarray = np.full(n, p.pid, dtype=object)
        if p.has_credit:
            credit_mask = rng.random(n) < p.credit_share
            acc = np.where(credit_mask, "credit", acc)
            orig = np.where(credit_mask, p.pid + "_Cred", orig)
        frames.append(
            _frame(
                step=ev_day * 24 + hours,
                type="SHOP",
                amount=np.round(amts, 2),
                nameOrig=orig,
                nameDest=np.asarray(["M_" + m["name"] for m in merchants]),
                merchant=np.asarray([m["name"] for m in merchants]),
                category=cat,
                subcategory=np.asarray([m["subcategory"] for m in merchants]),
                transaction_region=region,
                account_type=acc,
            )
        )
    if frames:
        return pd.concat(frames, ignore_index=True)
    return _frame(
        step=np.array([], dtype=int),
        type="SHOP",
        amount=np.array([], dtype=float),
        nameOrig=p.pid,
        nameDest="M_x",
        merchant="x",
        category="dining",
        subcategory="restaurants",
        transaction_region=p.home_region,
        account_type="checking",
    )


def _credit_autopay(
    rng: np.random.Generator,
    p: Persona,
    disc: pd.DataFrame,
    days: int,
    start: datetime,
) -> pd.DataFrame:
    """Monthly autopay covering the card's spend: checking debit + credit inflow."""
    empty = _frame(
        step=np.array([], dtype=int),
        type="TRANSFER",
        amount=np.array([], dtype=float),
        nameOrig=p.pid,
        nameDest=p.pid + "_Cred",
        merchant="Credit Card Autopay",
        category="credit",
        subcategory="credit_payment",
        transaction_region=p.home_region,
        account_type="checking",
    )
    if not p.has_credit or disc.empty:
        return empty
    credit_spend = disc[disc["account_type"] == "credit"]
    if credit_spend.empty:
        return empty
    pay_day = int(rng.integers(24, 29))
    months = (pd.to_datetime(start) + pd.to_timedelta(credit_spend["step"], unit="h")).dt.strftime(
        "%Y-%m"
    )
    monthly = credit_spend.assign(_m=months).groupby("_m")["amount"].sum()
    autopay_steps: list[int] = []
    autopay_amts: list[float] = []
    for mkey, amt in monthly.items():
        year_m, month_m = (int(x) for x in mkey.split("-"))
        target = datetime(year_m, month_m, min(pay_day, 28))
        day_off = (target - start).days
        if 0 <= day_off < days:
            autopay_steps.append(day_off * 24 + 10)
            autopay_amts.append(round2(amt))
    if not autopay_steps:
        return empty
    out = _frame(
        step=np.asarray(autopay_steps, dtype=int),
        type="TRANSFER",
        amount=np.asarray(autopay_amts, dtype=float),
        nameOrig=p.pid,
        nameDest=p.pid + "_Cred",
        merchant="Credit Card Autopay",
        category="credit",
        subcategory="credit_payment",
        transaction_region=p.home_region,
        account_type="checking",
    )
    cred = _frame(
        step=np.asarray(autopay_steps, dtype=int),
        type="CASH_IN",
        amount=np.asarray(autopay_amts, dtype=float),
        nameOrig=p.pid + "_Cred",
        nameDest=_CASH_ACCOUNT,
        merchant="Credit Card Payment",
        category="credit",
        subcategory="credit_payment",
        transaction_region=p.home_region,
        account_type="credit",
    )
    return pd.concat([out, cred], ignore_index=True)


def _regular_trips(
    rng: np.random.Generator, p: Persona, days: int, start: datetime
) -> pd.DataFrame:
    """1-2 legitimate trips per year to the persona's usual travel regions."""
    frames: list[pd.DataFrame] = []
    if not p.travel_regions:
        return _frame(
            step=np.array([], dtype=int),
            type="SHOP",
            amount=np.array([], dtype=float),
            nameOrig=p.pid,
            nameDest="M_x",
            merchant="x",
            category="dining",
            subcategory="restaurants",
            transaction_region=p.home_region,
            account_type="checking",
        )
    start_year = start.year
    for year in range(start_year, start_year + max(1, days // 365) + 1):
        if rng.random() > 0.85:
            continue
        trip_region = str(rng.choice(p.travel_regions))
        base_day = rng.integers(30, 335)
        # convert (year, day-of-year) to a day index within the window
        target = datetime(year, 1, 1) + timedelta(days=int(base_day))
        day_off = (target - start).days
        if not (0 <= day_off < days - 5):
            continue
        d0 = day_off
        n = int(rng.integers(2, 4))
        for i in range(n):
            cat = str(rng.choice(["dining", "transport", "entertainment"]))
            merch = sample_merchants(rng, cat, 1)[0]
            frames.append(
                _frame(
                    step=(d0 + i) * 24 + rng.integers(11, 22),
                    type="SHOP",
                    amount=np.round(rng.uniform(15.0, 90.0), 2),
                    nameOrig=p.pid,
                    nameDest="M_" + merch["name"],
                    merchant=merch["name"],
                    category=cat,
                    subcategory=merch["subcategory"],
                    transaction_region=trip_region,
                    account_type="checking",
                )
            )
    if frames:
        return pd.concat(frames, ignore_index=True)
    return _frame(
        step=np.array([], dtype=int),
        type="SHOP",
        amount=np.array([], dtype=float),
        nameOrig=p.pid,
        nameDest="M_x",
        merchant="x",
        category="dining",
        subcategory="restaurants",
        transaction_region=p.home_region,
        account_type="checking",
    )


def _big_txns(rng: np.random.Generator, p: Persona, days: int, month: np.ndarray) -> pd.DataFrame:
    """Occasional legitimate large purchases (small-business persona)."""
    n_months = max(1, days // 28)
    counts = rng.poisson(0.4, size=n_months)
    total = int(counts.sum())
    if total == 0:
        return _frame(
            step=np.array([], dtype=int),
            type="SHOP",
            amount=np.array([], dtype=float),
            nameOrig=p.pid,
            nameDest="M_x",
            merchant="x",
            category="shopping",
            subcategory="retail",
            transaction_region=p.home_region,
            account_type="checking",
        )
    ev_month = np.repeat(np.arange(n_months), counts)
    ev_day = np.clip(ev_month * 28 + rng.integers(1, 28, total), 0, days - 1)
    merch = sample_merchants(rng, "shopping", total)
    return _frame(
        step=ev_day * 24 + rng.integers(10, 18),
        type="SHOP",
        amount=np.round(rng.uniform(300.0, 1800.0, total), 2),
        nameOrig=p.pid,
        nameDest=np.asarray(["M_" + m["name"] for m in merch]),
        merchant=np.asarray([m["name"] for m in merch]),
        category="shopping",
        subcategory=np.asarray([m["subcategory"] for m in merch]),
        transaction_region=p.home_region,
        account_type="checking",
    )


def _inject_patterns(
    rng: np.random.Generator,
    p: Persona,
    days: int,
    start: datetime,
    bg_pool: list[str],
    scale: float,
) -> pd.DataFrame:
    """Fraud + hard-negative patterns, once per calendar year in the window."""
    frames: list[pd.DataFrame] = []
    used_merchants: set[str] = set()
    used_dests: set[str] = set()
    used_regions: set[str] = set()
    for year in range(start.year, start.year + max(1, days // 365) + 1):
        lo = (datetime(year, 1, 1) - start).days + 1
        hi = (datetime(year, 12, 31) - start).days + 1
        if hi < 1 or lo > days:
            continue
        lo = max(1, lo)
        hi = min(days, hi)
        ctx = fp.PatternCtx(
            rng=rng,
            persona=p,
            days=days,
            start=start,
            used_merchants=used_merchants,
            used_dests=used_dests,
            used_regions=used_regions,
            bg_pool=bg_pool,
            scale=scale,
            day_lo=lo,
            day_hi=hi,
        )
        rows = fp.inject_focal_patterns(ctx)
        if rows:
            frames.append(pd.DataFrame(rows))
    if frames:
        return pd.concat(frames, ignore_index=True)
    return _frame(
        step=np.array([], dtype=int),
        type="SHOP",
        amount=np.array([], dtype=float),
        nameOrig=p.pid,
        nameDest="M_x",
        merchant="x",
        category="dining",
        subcategory="restaurants",
        transaction_region=p.home_region,
        account_type="checking",
    )
