"""Persona population model (Data-Gen §2).

Each persona is a parameterized archetype that generates its own realistic
income / spending / timing distributions. Parameters are randomized per
individual around the archetype prior, so no two "young professionals" spend
identically. Sampling is fully seeded via per-persona RNG substreams (a
single top-level seed -> SeedSequence -> one substream per persona), so a
generation order change for one persona can never perturb another's data
(§9.4 reproducibility contract).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from finance_agent.merchants import REGION_IDS, SUBSCRIPTION_AMOUNTS

DISCRETIONARY_CATEGORIES = [
    "groceries",
    "dining",
    "transport",
    "entertainment",
    "shopping",
    "health",
]

ARCHETYPE_NAMES = [
    "young_professional",
    "dual_income_family",
    "gig_worker",
    "retiree",
    "recent_graduate",
    "small_business_owner",
]

# Archetype priors — ranges are sampled per individual. ``cadence`` is the
# income rhythm; "irregular" means lumpy CASH_IN events instead of a payroll
# schedule. ``cat_prior`` weights the Dirichlet draw over discretionary spend.
_ARCHETYPE_SPECS: dict[str, dict] = {
    "young_professional": {
        "income": (55000.0, 95000.0),
        "cadence": ["biweekly", "semimonthly"],
        "rent_share": (0.28, 0.36),
        "savings_rate": (0.10, 0.16),
        "cat_prior": {
            "groceries": 0.22,
            "dining": 0.30,
            "transport": 0.12,
            "entertainment": 0.14,
            "shopping": 0.14,
            "health": 0.08,
        },
        "subs": (3, 6),
        "cash_in_rate": (0.8, 1.2),  # freelance deposits per month
        "cash_in_mean": (350.0, 900.0),
        "has_credit": True,
        "credit_share": (0.3, 0.5),
        "spend_multiplier": (0.9, 1.15),
        "velocity": (1.6, 2.6),  # discretionary transactions per day
        "p2p_share": 0.5,  # bill-splitting transfers
    },
    "dual_income_family": {
        "income": (110000.0, 180000.0),
        "cadence": ["semimonthly", "monthly"],
        "rent_share": (0.24, 0.30),
        "savings_rate": (0.08, 0.14),
        "cat_prior": {
            "groceries": 0.34,
            "dining": 0.16,
            "transport": 0.10,
            "entertainment": 0.10,
            "shopping": 0.20,
            "health": 0.10,
        },
        "subs": (4, 8),
        "cash_in_rate": (0.0, 0.1),
        "cash_in_mean": (0.0, 100.0),
        "has_credit": True,
        "credit_share": (0.2, 0.35),
        "spend_multiplier": (1.0, 1.2),
        "velocity": (2.4, 3.6),
        "p2p_share": 0.2,
    },
    "gig_worker": {
        "income": (30000.0, 70000.0),
        "cadence": ["irregular"],
        "rent_share": (0.30, 0.42),
        "savings_rate": (0.02, 0.06),
        "cat_prior": {
            "groceries": 0.24,
            "dining": 0.26,
            "transport": 0.18,
            "entertainment": 0.10,
            "shopping": 0.14,
            "health": 0.08,
        },
        "subs": (1, 3),
        "cash_in_rate": (3.0, 7.0),  # per week — first-class CASH_IN events
        "cash_in_mean": (150.0, 500.0),
        "has_credit": False,
        "credit_share": 0.0,
        "spend_multiplier": (0.7, 1.0),
        "velocity": (1.0, 1.8),
        "p2p_share": 0.3,
        "thin_buffer": True,
    },
    "retiree": {
        "income": (28000.0, 55000.0),
        "cadence": ["monthly"],
        "rent_share": (0.15, 0.25),
        "savings_rate": (0.05, 0.10),
        "cat_prior": {
            "groceries": 0.30,
            "dining": 0.10,
            "transport": 0.06,
            "entertainment": 0.12,
            "shopping": 0.12,
            "health": 0.30,
        },
        "subs": (1, 3),
        "cash_in_rate": (0.0, 0.0),
        "cash_in_mean": (0.0, 0.0),
        "has_credit": False,
        "credit_share": 0.0,
        "spend_multiplier": (0.5, 0.8),
        "velocity": (0.5, 1.0),
        "p2p_share": 0.05,
    },
    "recent_graduate": {
        "income": (45000.0, 70000.0),
        "cadence": ["monthly", "biweekly"],
        "rent_share": (0.34, 0.42),
        "savings_rate": (0.02, 0.06),
        "cat_prior": {
            "groceries": 0.22,
            "dining": 0.26,
            "transport": 0.10,
            "entertainment": 0.14,
            "shopping": 0.16,
            "health": 0.12,
        },
        "subs": (2, 4),
        "cash_in_rate": (0.3, 0.7),
        "cash_in_mean": (150.0, 400.0),
        "has_credit": True,
        "credit_share": (0.3, 0.45),
        "spend_multiplier": (0.8, 1.0),
        "velocity": (1.3, 2.2),
        "p2p_share": 0.3,
        "loan": True,
        "loan_monthly": (350.0, 900.0),
    },
    "small_business_owner": {
        "income": (60000.0, 120000.0),
        "cadence": ["irregular"],
        "rent_share": (0.20, 0.30),
        "savings_rate": (0.10, 0.20),
        "cat_prior": {
            "groceries": 0.18,
            "dining": 0.20,
            "transport": 0.16,
            "entertainment": 0.08,
            "shopping": 0.28,
            "health": 0.10,
        },
        "subs": (2, 4),
        "cash_in_rate": (2.0, 5.0),
        "cash_in_mean": (800.0, 4000.0),
        "has_credit": True,
        "credit_share": (0.3, 0.5),
        "spend_multiplier": (1.0, 1.35),
        "velocity": (1.2, 2.2),
        "p2p_share": 0.4,
        "big_txn": True,
    },
}


@dataclass
class Persona:
    """A fully-parameterized focal-equivalent individual."""

    pid: str
    archetype: str
    annual_income_start: float
    income_cadence: str  # weekly | biweekly | semimonthly | monthly | irregular
    payday_dom: int | None  # day-of-month paydays (monthly/semimonthly)
    payday_weekday: int | None  # weekday paydays (weekly)
    rent_monthly: float
    savings_rate: float
    category_weights: dict[str, float]
    subscriptions: list[str]
    col_multiplier: float
    spend_multiplier: float
    velocity: float
    has_credit: bool
    credit_share: float
    cash_in_rate: float  # CASH_IN events per month (0 for salaried-only)
    cash_in_mean: float
    loan_monthly: float  # fixed debt payment (0 for most personas)
    home_region: str
    travel_regions: list[str]
    raises: list[float]  # annual raise per year index (year 0 = first year)
    opening_balance: float
    monthly_fixed: float  # rent + utilities + subscriptions + loan estimate
    monthly_discretionary: float
    p2p_share: float
    thin_buffer: bool = False
    big_txn: bool = False
    employer: str = "Acme Corp Payroll"
    accounts: list[str] = field(default_factory=list)  # checking id, savings id, credit id

    @property
    def monthly_income(self) -> float:
        return round(self.annual_income_start / 12.0, 2)


def assign_archetypes(n: int, rng: np.random.Generator, first: str | None = None) -> list[str]:
    """Assign archetypes to `n` personas; every archetype appears once n >= 6.

    ``first`` pins the first persona's archetype (used so the app's default
    user is always a young professional — the archetype with the richest
    CASH_IN / P2P / credit behaviour).
    """
    base = list(_ARCHETYPE_SPECS)
    if n >= len(base):
        picks = base + [str(rng.choice(base)) for _ in range(n - len(base))]
    else:
        picks = [str(rng.choice(base)) for _ in range(n)]
    if first is not None and n >= 1:
        picks[0] = first
    rng.shuffle(picks)
    return picks


def _sample_subscriptions(rng: np.random.Generator, lo: int, hi: int) -> list[str]:
    names = sorted(SUBSCRIPTION_AMOUNTS)
    k = int(rng.integers(lo, hi + 1))
    k = min(k, len(names))
    return [str(n) for n in rng.choice(names, size=k, replace=False)]


def sample_persona(pid: str, archetype: str, rng: np.random.Generator) -> Persona:
    """Draw one persona from its archetype prior using its own RNG substream."""
    spec = _ARCHETYPE_SPECS[archetype]
    income = float(rng.uniform(*spec["income"]))
    cadence = str(rng.choice(spec["cadence"]))
    payday_dom: int | None = None
    payday_weekday: int | None = None
    if cadence == "monthly":
        payday_dom = int(rng.integers(1, 4))
    elif cadence == "semimonthly":
        payday_dom = int(rng.integers(1, 4))  # 1st/15th anchored to this day
    elif cadence == "weekly":
        payday_weekday = int(rng.integers(0, 5))

    col = float(rng.uniform(0.85, 1.25))
    rent = round(income / 12.0 * float(rng.uniform(*spec["rent_share"])) * col, 2)
    savings_rate = float(rng.uniform(*spec["savings_rate"]))
    prior = spec["cat_prior"]
    weights = rng.dirichlet(np.asarray([prior[c] * 8.0 for c in DISCRETIONARY_CATEGORIES]))
    cat_weights = {c: float(w) for c, w in zip(DISCRETIONARY_CATEGORIES, weights, strict=True)}
    subs = _sample_subscriptions(rng, *spec["subs"])
    cash_rate = float(rng.uniform(*spec["cash_in_rate"]))
    cash_mean = float(rng.uniform(*spec["cash_in_mean"]))
    loan = float(rng.uniform(*spec.get("loan_monthly", (0.0, 0.0)))) if spec.get("loan") else 0.0

    monthly_income = income / 12.0
    sub_total = sum(SUBSCRIPTION_AMOUNTS[s] for s in subs) * col
    util_est = 130.0 * col
    monthly_fixed = rent + sub_total + util_est + loan
    monthly_discretionary = max(0.0, monthly_income * 0.92 - monthly_fixed)

    # Annual raises: 2-5% per year, deterministic per persona.
    n_years = 6
    raises = [float(rng.uniform(0.02, 0.05)) for _ in range(n_years)]

    # A persona's home region + a couple of usual travel regions.
    pool = list(REGION_IDS)
    rng.shuffle(pool)
    home_region = str(pool[0])
    travel_regions = [str(r) for r in pool[1:3]]

    spend_mult = float(rng.uniform(*spec["spend_multiplier"]))
    velocity = float(rng.uniform(*spec["velocity"]))
    has_credit = bool(spec["has_credit"])
    credit_share = float(rng.uniform(*spec["credit_share"])) if has_credit else 0.0
    thin = bool(spec.get("thin_buffer", False))
    big = bool(spec.get("big_txn", False))
    p2p_share = float(spec.get("p2p_share", 0.2))

    # Opening balance: 0.5-3 months of expenses (thin-buffer personas lower).
    months_open = float(rng.uniform(0.5, 1.5)) if thin else float(rng.uniform(1.0, 3.0))
    opening = round(months_open * (monthly_fixed + monthly_discretionary), 2)

    accounts = [pid, f"{pid}_Sav"]
    if has_credit:
        accounts.append(f"{pid}_Cred")

    return Persona(
        pid=pid,
        archetype=archetype,
        annual_income_start=round(income, 2),
        income_cadence=cadence,
        payday_dom=payday_dom,
        payday_weekday=payday_weekday,
        rent_monthly=rent,
        savings_rate=savings_rate,
        category_weights=cat_weights,
        subscriptions=subs,
        col_multiplier=col,
        spend_multiplier=spend_mult,
        velocity=velocity,
        has_credit=has_credit,
        credit_share=credit_share,
        cash_in_rate=cash_rate,
        cash_in_mean=cash_mean,
        loan_monthly=loan,
        home_region=home_region,
        travel_regions=travel_regions,
        raises=raises,
        opening_balance=opening,
        monthly_fixed=round(monthly_fixed, 2),
        monthly_discretionary=round(monthly_discretionary, 2),
        p2p_share=p2p_share,
        thin_buffer=thin,
        big_txn=big,
        accounts=accounts,
    )


def sample_personas(user_ids: list[str], seed: int, rng: np.random.Generator) -> list[Persona]:
    """Sample one persona per user id, each from its own seeded substream."""
    archetypes = assign_archetypes(len(user_ids), rng)
    ss = np.random.SeedSequence(seed)
    children = ss.spawn(len(user_ids))
    out: list[Persona] = []
    for i, (pid, arch) in enumerate(zip(user_ids, archetypes, strict=True)):
        out.append(sample_persona(pid, arch, np.random.default_rng(children[i])))
    return out


# --------------------------------------------------------- background pool
@dataclass
class BackgroundProfile:
    """A lightweight background account (same persona system, less detail)."""

    pid: str
    archetype: str
    opening: float
    monthly_income: float
    cadence: str  # monthly | irregular
    income_cash_rate: float  # CASH_IN events per month (irregular)
    income_cash_mean: float
    spend_rate: float  # spending transactions per day
    cat_weights: dict[str, float]
    bust_out: bool  # carries the bust-out fraud archetype (§5 pattern 11)
    region: str = "R00_portland"
    travel_regions: list[str] = field(default_factory=list)


def _region_sample(rng: np.random.Generator) -> tuple[str, list[str]]:
    pool = list(REGION_IDS)
    rng.shuffle(pool)
    return str(pool[0]), [str(r) for r in pool[1:3]]


def sample_background(
    n: int,
    seed: int,
    rng: np.random.Generator,
    bust_fraction: float = 0.01,
) -> list[BackgroundProfile] -> None:
    """Sample `n` background profiles at scale (batched, not object-heavy)."""
    archetypes = assign_archetypes(n, rng)
    ss = np.random.SeedSequence(seed)
    # Hoist the spawn: spawning n children inside the loop would be O(n^2)
    # hashing work (the bench tier samples 20k profiles). The children are
    # identical either way, so per-profile streams (and therefore output) are
    # byte-for-byte the same as a per-loop `ss.spawn(n)[i]`.
    children = ss.spawn(n)
    out: list[BackgroundProfile] = []
    for i in range(n):
        r = np.random.default_rng(children[i])
        arch = archetypes[i]
        spec = _ARCHETYPE_SPECS[arch]
        income = float(r.uniform(*spec["income"]))
        opening = float(r.lognormal(7.5, 1.2))  # ~$200 .. $200k as before
        cadence = str(r.choice(spec["cadence"]))
        cash_lo, cash_hi = (float(v) for v in spec["cash_in_mean"])
        cash_rate = float(r.uniform(*spec["cash_in_rate"]))
        cash_mean = float(r.uniform(cash_lo, cash_hi)) if cash_hi > 0 else 0.0
        prior = spec["cat_prior"]
        weights = r.dirichlet(np.asarray([prior[c] * 8.0 for c in DISCRETIONARY_CATEGORIES]))
        cat_weights = {c: float(w) for c, w in zip(DISCRETIONARY_CATEGORIES, weights, strict=True)}
        spend_rate = float(r.uniform(0.03, 0.12)) * (1.2 if arch == "dual_income_family" else 1.0)
        bust = bool(r.random() < bust_fraction) if arch != "retiree" else False
        region, travel = _region_sample(r)
        out.append(
            BackgroundProfile(
                pid=f"C_BG{i:06d}",
                archetype=arch,
                opening=round(opening, 2),
                monthly_income=round(income / 12.0, 2),
                cadence=cadence,
                income_cash_rate=cash_rate,
                income_cash_mean=cash_mean,
                spend_rate=spend_rate,
                cat_weights=cat_weights,
                bust_out=bust,
                region=region,
                travel_regions=travel,
            )
        )
    return out


def avg_amount_by_category(category: str, col_multiplier: float) -> float:
    """Typical transaction amount for a category (used for amount draws)."""
    base = {
        "groceries": 62.0,
        "dining": 34.0,
        "transport": 11.0,
        "entertainment": 42.0,
        "shopping": 58.0,
        "health": 38.0,
    }
    return base.get(category, 40.0) * col_multiplier


def amount_sigma_by_category(category: str) -> float:
    """Lognormal sigma for a category's amount distribution."""
    return {
        "groceries": 0.55,
        "dining": 0.5,
        "transport": 0.55,
        "entertainment": 0.6,
        "shopping": 0.65,
        "health": 0.55,
    }.get(category, 0.55)


def round2(x: float) -> float:
    return round(float(x), 2) if math.isfinite(x) else 0.0
