"""Datagen — Per-persona ledger generation (main generation loop)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from finance_agent import fraud_patterns as fp
from finance_agent.datagen_pkg.config import _CASH_ACCOUNT, _INFLATION, SUB_SUBCATEGORY
from finance_agent.datagen_pkg.helpers import (
    _CLUSTER,
    _PERIODS,
    _days_since_payday,
    _frame,
    _payday_mask,
    _pick_merchant,
    _raise_multipliers,
)
from finance_agent.datagen_pkg.transactions import (
    _big_txns,
    _credit_autopay,
    _discretionary,
    _inject_patterns,
    _regular_trips,
    _salaried_cashin,
    _savings_transfer,
    _spend_frame,
)
from finance_agent.merchants import SUBCATEGORIES, SUBSCRIPTION_AMOUNTS, seasonal_multiplier
from finance_agent.personas import DISCRETIONARY_CATEGORIES, Persona, round2

def _persona_ledger(
    rng: np.random.Generator,
    p: Persona,
    days: int,
    start: datetime,
    bg_pool: list[str],
    scale: float,
) -> pd.DataFrame:
    """One persona's full ledger as partial rows (balances applied later)."""
    frames: list[pd.DataFrame] = []
    day_idx = np.arange(days)
    dates = pd.to_datetime(start) + pd.to_timedelta(day_idx, unit="D")
    dom = dates.day.to_numpy()
    weekday = dates.weekday.to_numpy()
    month = dates.month.to_numpy()
    years_since = dates.year.to_numpy() - start.year
    infl = np.power(1.0 + _INFLATION, years_since)
    raise_mult = _raise_multipliers(years_since, p.raises)
    payday_days: np.ndarray = np.array([], dtype=int)

    # ---- income -----------------------------------------------------------
    if p.income_cadence == "irregular":
        weeks = max(1, int(np.ceil(days / 7)))
        counts = rng.poisson(p.cash_in_rate, size=weeks)
        total = int(counts.sum())
        if total > 0:
            ev_week = np.repeat(np.arange(weeks), counts)
            ev_day = np.clip(ev_week * 7 + rng.integers(0, 7, total), 0, days - 1)
            amts = np.clip(
                rng.lognormal(np.log(max(p.cash_in_mean, 1.0)), 0.6, total), 20.0, 20000.0
            )
            frames.append(
                _frame(
                    step=ev_day * 24 + rng.integers(8, 14, total),
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
            )
        # monthly savings transfer on dom 2 (irregular income is lumpy)
        sav_mask = dom == 2
        frames.append(
            _savings_transfer(
                p,
                np.nonzero(sav_mask)[0] * 24 + 10,
                np.full(int(sav_mask.sum()), p.savings_rate * p.monthly_income),
            )
        )
    else:
        payday_mask = _payday_mask(p, day_idx, dom, weekday, rng)
        payday_days = np.nonzero(payday_mask)[0]
        base = p.annual_income_start / _PERIODS[p.income_cadence]
        amts = base * raise_mult[payday_days]
        frames.append(
            _frame(
                step=payday_days * 24 + 8,
                type="SALARY",
                amount=np.round(amts, 2),
                nameOrig=p.pid,
                nameDest="C_" + p.employer,
                merchant=p.employer,
                category="income",
                subcategory="payroll",
                transaction_region=p.home_region,
                account_type="checking",
            )
        )
        # savings transfer the hour after each payday
        sav_amts = amts * p.savings_rate
        frames.append(_savings_transfer(p, payday_days * 24 + 9, sav_amts))
        # freelance/refund CASH_IN for salaried personas (guaranteed >= 1)
        if p.cash_in_rate > 0:
            frames.append(_salaried_cashin(rng, p, days, dom, month, start, raise_mult))

    # ---- fixed monthly obligations ---------------------------------------
    rent_dom = int(rng.integers(3, 8))
    rm = dom == rent_dom
    rent_merchant = _pick_merchant(rng, "housing", "rent")
    frames.append(
        _spend_frame(
            p,
            np.nonzero(rm)[0] * 24 + 9,
            p.rent_monthly * infl[rm],
            "TRANSFER",
            p.pid + "_Landlord",
            "housing",
            "rent",
            rent_merchant["name"],
            dest_is_tracked=True,
        )
    )
    util_dom = int(rng.integers(15, 19))
    um = dom == util_dom
    season = np.array([seasonal_multiplier("utilities", m) for m in month])
    util_sub = str(rng.choice(SUBCATEGORIES["utilities"]))
    util_merchant = _pick_merchant(rng, "utilities", util_sub)
    frames.append(
        _spend_frame(
            p,
            np.nonzero(um)[0] * 24 + 11,
            130.0 * p.col_multiplier * season[um] * infl[um],
            "PAYMENT",
            "C_" + util_merchant["name"],
            "utilities",
            util_sub,
            util_merchant["name"],
        )
    )
    sub_dom = int(rng.integers(5, 13))
    sm = dom == sub_dom
    for sub in p.subscriptions:
        amt = SUBSCRIPTION_AMOUNTS[sub] * p.col_multiplier * infl[sm]
        frames.append(
            _spend_frame(
                p,
                np.nonzero(sm)[0] * 24 + 10,
                amt,
                "SUBSCRIPTION",
                "C_" + sub,
                "subscriptions",
                SUB_SUBCATEGORY.get(sub, "streaming"),
                sub,
            )
        )
    if p.loan_monthly > 0:
        loan_dom = int(rng.integers(8, 15))
        lm = dom == loan_dom
        frames.append(
            _spend_frame(
                p,
                np.nonzero(lm)[0] * 24 + 9,
                p.loan_monthly * infl[lm],
                "PAYMENT",
                "C_Student Loan Servicer",
                "housing",
                "mortgage",
                "Student Loan Servicer",
            )
        )

    # ---- discretionary spend (vectorized per category) --------------------
    disc = _discretionary(rng, p, days, dom, month, payday_days, day_idx)
    frames.append(disc)

    # ---- credit-card autopay (pays the month's card spend in full) --------
    frames.append(_credit_autopay(rng, p, disc, days, start))

    # ---- P2P bill-splitting transfers -------------------------------------
    n_months = max(1, days // 28)
    p2p_counts = rng.poisson(max(0.0, 2.0 * p.p2p_share), size=n_months)
    if int(p2p_counts.sum()) > 0:
        ev_month = np.repeat(np.arange(n_months), p2p_counts)
        ev_day = np.clip(ev_month * 28 + rng.integers(1, 28, int(p2p_counts.sum())), 0, days - 1)
        friend = f"C_Friend_{int(rng.integers(1000, 9999))}"
        frames.append(
            _frame(
                step=ev_day * 24 + rng.integers(12, 20),
                type="TRANSFER",
                amount=np.round(rng.uniform(12.0, 65.0, int(p2p_counts.sum())), 2),
                nameOrig=p.pid,
                nameDest=friend,
                merchant="Peer Transfer",
                category="transfer",
                subcategory="p2p",
                transaction_region=p.home_region,
                account_type="checking",
            )
        )

    # ---- legit travel to usual regions + big business txns ----------------
    frames.append(_regular_trips(rng, p, days, start))
    if p.big_txn:
        frames.append(_big_txns(rng, p, days, month))

    # ---- fraud / hard-negative patterns (once per calendar year) ----------
    frames.append(_inject_patterns(rng, p, days, start, bg_pool, scale))

    out = pd.concat(frames, ignore_index=True)
    return out
