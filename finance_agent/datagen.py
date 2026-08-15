"""Vectorized synthetic-data generator (Data-Gen §1, §3, §6, §7, §9).

This is the engine behind ``generate_data.py``. It replaces the old per-row
``Account``-object loop with **vectorized per-persona generation**:

  * every persona's full transaction stream is drawn with NumPy array
    operations over the whole time span (payday arithmetic, Poisson draws per
    category per day, lognormal amounts) — no ``iterrows()`` / ``.apply(axis=1)``
    anywhere in the hot path;
  * balances are applied with a per-account **clamped cumulative sum**
    (Lindley's recursion, vectorized per account) instead of a Python loop,
    and balance-dependent fraud amounts (drains) are resolved in a second pass;
  * reproducibility uses ``SeedSequence`` substreams — one per persona — so a
    reordering in the code can never change another persona's data (§9.4);
  * the multi-account structure (checking / savings / credit) makes internal
    transfers first-class rows, and the background population reuses the same
    persona system at reduced detail (§7).

``generate_dataset()`` returns the final DataFrame; ``generate_data.py`` owns
the CLI, tier defaults, and CSV/Parquet output.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from finance_agent import fraud_patterns as fp
from finance_agent.constants import CREDIT_TYPES
from finance_agent.merchants import (
    CATEGORY_GROUP,
    MERCHANTS,
    SUBCATEGORIES,
    SUBSCRIPTION_AMOUNTS,
    sample_merchants,
    seasonal_multiplier,
)
from finance_agent.personas import (
    DISCRETIONARY_CATEGORIES,
    BackgroundProfile,
    Persona,
    amount_sigma_by_category,
    avg_amount_by_category,
    round2,
    sample_background,
    sample_personas,
)

# Catalog index (module-level: merchants is import-only, no cycle).
MERCHANTS_INDEX: list[dict[str, str]] = list(MERCHANTS)

# ---- tier defaults ----------------------------------------------------------
# `tiny` matches the legacy footprint (fast tests + CI); `demo` is the app /
# README tier; `bench` is the model_bench tier — medium/hard fraud rates scale
# down so the class imbalance lands in a defensible real-world-adjacent range.
# Background fraud is driven entirely by `bust_fraction` (pattern 11); there
# is no separate non-bust background fraud injection rate. `days` is the
# simulation window the CLI uses when the tier owns the span (bench must span
# multiple years, Data-Gen §3); `focal_users` is the default focal population
# (bench ships 200 focal personas so the fraud rate stays in the defensible
# 0.1–0.5% band despite the huge legitimate background ledger, Data-Gen §1/§5).
TIER_DEFAULTS: dict[str, dict[str, float | int]] = {
    "tiny": {
        "background": 20,
        "fraud_scale": 1.0,
        "bust_fraction": 0.0,
        "days": 90,
        "focal_users": 1,
    },
    "demo": {
        "background": 2000,
        "fraud_scale": 1.0,
        "bust_fraction": 0.01,
        "days": 90,
        "focal_users": 1,
    },
    "bench": {
        "background": 20000,
        "fraud_scale": 0.5,
        "bust_fraction": 0.008,
        "days": 1460,
        "focal_users": 200,
    },
}

FOCAL_NAMES: list[str] = [
    "Alex",
    "Maria",
    "Noah",
    "Priya",
    "Chen",
    "Sofia",
    "Liam",
    "Aisha",
    "Diego",
    "Elena",
    "Omar",
    "Hana",
    "Mateo",
    "Ingrid",
    "Kwame",
    "Yuki",
    "Felix",
    "Nora",
    "Ravi",
    "Clara",
    "Jonas",
    "Zara",
    "Amir",
    "Lena",
    "Theo",
]

SUB_SUBCATEGORY: dict[str, str] = {
    "Netflix": "streaming",
    "Max (streaming)": "streaming",
    "YouTube Premium": "streaming",
    "Spotify": "software",
    "Adobe CC": "software",
    "iCloud": "software",
    "Planet Fitness": "fitness",
    "Verizon": "phone",
}

_INFLATION = 0.025  # annual rent/utilities/subscription drift
_CASH_ACCOUNT = "C_External"

# Legacy column order first (existing consumers), then the new additive columns.
LEGACY_COLUMNS = [
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
NEW_COLUMNS = [
    "persona_id",
    "persona_archetype",
    "account_type",
    "merchant_region",
    "transaction_region",
    "home_region",
    "category_group",
    "subcategory",
    "fraud_archetype",
    "label_reported_at_step",
    "simulation_year",
]
FINAL_COLUMNS = LEGACY_COLUMNS + NEW_COLUMNS

PARTIAL_COLUMNS = [
    "step",
    "type",
    "amount",
    "nameOrig",
    "nameDest",
    "merchant",
    "category",
    "subcategory",
    "transaction_region",
    "account_type",
    "isFraud",
    "is_anomaly",
    "anomaly_type",
    "fraud_archetype",
    "label_reported_at_step",
]


def focal_user_ids(n: int, existing: list[str] | None = None) -> list[str]:
    """Persona ids: `existing` first (legacy names preserved), then the name pool."""
    ids = [str(u) for u in (existing or [])]
    for name in FOCAL_NAMES:
        if len(ids) >= n:
            break
        cand = f"U_{name}"
        if cand not in ids:
            ids.append(cand)
    k = 1
    while len(ids) < n:
        cand = f"U_Persona{k:02d}"
        if cand not in ids:
            ids.append(cand)
        k += 1
    return ids[:n]


# ------------------------------------------------------------------ helpers
def _raise_multipliers(years_since: np.ndarray, raises: list[float]) -> np.ndarray:
    """Per-year income multiplier: prod(1 + raise) for each year index."""
    mult = np.ones(len(raises) + 1, dtype=float)
    for i, r in enumerate(raises):
        mult[i + 1] = mult[i] * (1.0 + r)
    idx = np.clip(years_since, 0, len(raises)).astype(int)
    return mult[idx]


def _pick_merchant(rng: np.random.Generator, category: str, subcategory: str) -> dict[str, str]:
    """A catalog merchant of the requested (category, subcategory)."""
    idx = [
        i
        for i, m in enumerate(MERCHANTS_INDEX)
        if m["category"] == category and m["subcategory"] == subcategory
    ]
    return MERCHANTS_INDEX[int(rng.choice(idx))]


def _frame(**cols: Any) -> pd.DataFrame:
    """Build a partial-row DataFrame from column arrays (mixed scalars allowed)."""
    n: int | None = None
    for v in cols.values():
        if isinstance(v, (list, np.ndarray, pd.Series)):
            n = len(v)
            break
    if n is None:
        # Single-row path (all scalar columns) must get the same partial-column
        # defaults, or rows from e.g. _regular_trips would miss isFraud etc.
        df = pd.DataFrame([{k: v for k, v in cols.items()}])
        for c in PARTIAL_COLUMNS:
            if c not in df.columns:
                df[c] = 0 if c in ("isFraud", "is_anomaly") else ""
        return df
    out: dict[str, Any] = {}
    for k, v in cols.items():
        if isinstance(v, (list, np.ndarray, pd.Series)):
            out[k] = np.asarray(v)
        else:
            out[k] = np.repeat(v, n) if n > 0 else np.array([], dtype=object)
    df = pd.DataFrame(out)
    for c in PARTIAL_COLUMNS:
        if c not in df.columns:
            df[c] = 0 if c in ("isFraud", "is_anomaly") else ""
    return df


# ------------------------------------------------------------------ persona
def _payday_mask(
    p: Persona, day_idx: np.ndarray, dom: np.ndarray, weekday: np.ndarray, rng: np.random.Generator
) -> np.ndarray:
    if p.income_cadence == "monthly":
        return dom == p.payday_dom
    if p.income_cadence == "semimonthly":
        second = min(28, int(p.payday_dom or 1) + 14)
        return (dom == p.payday_dom) | (dom == second)
    if p.income_cadence == "weekly":
        return weekday == p.payday_weekday
    offset = int(rng.integers(0, 14))  # biweekly anchor (deterministic order)
    return (day_idx - offset) % 14 == 0


_PERIODS = {"monthly": 12, "semimonthly": 24, "biweekly": 26, "weekly": 52}


def _days_since_payday(payday_days: np.ndarray, day_idx: np.ndarray) -> np.ndarray:
    """0-based days since the most recent payday (capped at the cluster table)."""
    if payday_days.size == 0:
        return np.full(day_idx.size, 6, dtype=int)
    pos = np.searchsorted(payday_days, day_idx, side="right") - 1
    out = day_idx - np.where(pos >= 0, payday_days[np.clip(pos, 0, None)], day_idx - 6)
    return np.clip(out, 0, 6)


_CLUSTER = {0: 0.55, 1: 2.0, 2: 1.6, 3: 1.35, 4: 1.15, 5: 1.0, 6: 0.95}


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


# ------------------------------------------------------------------ background
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


# ------------------------------------------------------------------ assembly
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
