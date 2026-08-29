"""Fraud & anomaly pattern library (Data-Gen §5) — 15 difficulty-graded archetypes.

Each pattern is a pure generator function taking a ``PatternCtx`` and returning
a list of *partial row dicts* (tags + timestamps + amounts; balance fields are
filled later by the vectorized balance pass). Every pattern is deterministic
given the ctx's RNG substream and carries a ``fraud_archetype`` slug so the
benchmark can report **per-archetype recall** instead of one aggregate number.

Difficulty tiers (see docs/DataGeneration.md):

  * easy — rule-detectable: 1 balance_drain, 2 duplicate_charge, 3 spend_spike
  * medium — needs the supervised model: 4 card_testing, 5 slow_balance_drain,
    6 new_payee_transfer, 7 subscription_creep, 8 refund_abuse
  * hard / adversarial — deliberately imperfect recall: 9 mimicry,
    10 account_takeover, 11 bust_out (background), 12 seasonal_mimicry
  * hard negatives — NOT fraud but must resemble it: 13 life_event, 14 travel,
    15 rapid_burst

Label realism: a small discovery-lag rate (fraud not knowable until N days
later) is applied by :func:`apply_discovery_lag`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from finance_agent.merchants import (
    REGION_IDS,
)
from finance_agent.personas import (
    Persona,
    amount_sigma_by_category,
    avg_amount_by_category,
    round2,
)

# Per-persona-per-year injection rates. Easy patterns (1-3) are deterministic
# when the window fits; medium/hard fire with these probabilities. The tier
# system scales medium/hard rates down for `bench` (see datagen.py).
PATTERN_RATES: dict[str, float] = {
    "balance_drain": 1.0,  # deterministic
    "duplicate_charge": 1.0,
    "spend_spike": 1.0,
    "card_testing": 0.9,
    "slow_balance_drain": 0.65,
    "new_payee_transfer": 0.65,
    "subscription_creep": 0.5,
    "refund_abuse": 0.4,
    "mimicry": 0.7,
    "account_takeover": 0.3,
    "bust_out": 1.0,  # deterministic when a background account is flagged (bust_fraction)
    "seasonal_mimicry": 0.3,
    # hard negatives
    "life_event": 0.6,
    "travel": 1.0,
    "rapid_burst": 0.8,
}

EASY_PATTERNS = ["balance_drain", "duplicate_charge", "spend_spike"]
MEDIUM_PATTERNS = [
    "card_testing",
    "slow_balance_drain",
    "new_payee_transfer",
    "subscription_creep",
    "refund_abuse",
]
HARD_PATTERNS = ["mimicry", "account_takeover", "seasonal_mimicry"]
HARD_NEGATIVES = ["life_event", "travel", "rapid_burst"]

# Minimum window (days) below which a pattern has no room to play out.
_MIN_DAYS: dict[str, int] = {
    "balance_drain": 6,
    "duplicate_charge": 12,
    "spend_spike": 15,
    "card_testing": 8,
    "slow_balance_drain": 30,
    "new_payee_transfer": 12,
    "subscription_creep": 21,
    "refund_abuse": 60,
    "mimicry": 15,
    "account_takeover": 60,
    "bust_out": 120,
    "seasonal_mimicry": 45,
    "life_event": 30,
    "travel": 20,
    "rapid_burst": 10,
}

# ---- convenience sets -------------------------------------------------------
_CASH_ACCOUNT = "C_External"
_ATM = "C_ATM"


@dataclass
class PatternCtx:
    """Everything a pattern generator needs about one persona + its history."""

    rng: np.random.Generator
    persona: Persona
    days: int
    start: datetime
    used_merchants: set[str] = field(default_factory=set)
    used_dests: set[str] = field(default_factory=set)
    used_regions: set[str] = field(default_factory=set)
    bg_pool: list[str] = field(default_factory=list)
    scale: float = 1.0  # tier multiplier for medium/hard rates
    day_lo: int = 1  # injection window (1-based day) — multi-year support
    day_hi: int | None = None

    def __post_init__(self) -> None:
        self.used_regions.add(self.persona.home_region)
        self.used_regions.update(self.persona.travel_regions)
        if self.day_hi is None:
            self.day_hi = self.days

    def _hi(self) -> int:
        """Effective injection-window end day (None resolves to the full span)."""
        return self.days if self.day_hi is None else self.day_hi

    @property
    def window_days(self) -> int:
        return max(0, self._hi() - self.day_lo + 1)

    # -- helpers ---------------------------------------------------------
    def step_of(self, day: int, hour: int = 12) -> int:
        return (day - 1) * 24 + hour

    def date_of(self, day: int) -> datetime:
        return self.start + timedelta(days=day - 1)

    def day_in(self, lo: int, hi: int) -> int:
        lo = max(self.day_lo, lo)
        hi = min(self._hi(), self.days, hi)
        if hi < lo:
            return self.day_lo
        return int(self.rng.integers(lo, hi + 1))

    def fresh_region(self) -> str:
        """A region this persona has never used (registers it)."""
        candidates = [r for r in REGION_IDS if r not in self.used_regions]
        if not candidates:
            candidates = [r for r in REGION_IDS if r != self.persona.home_region]
        pick = str(self.rng.choice(candidates))
        self.used_regions.add(pick)
        return pick

    def category_amount(self, category: str) -> float:
        """Draw an amount from this persona's distribution for `category`."""
        mean = avg_amount_by_category(category, self.persona.col_multiplier)
        sigma = amount_sigma_by_category(category)
        amt = float(np.clip(self.rng.lognormal(np.log(mean), sigma), mean * 0.25, mean * 4.0))
        return round2(amt)


def _row(
    ctx: PatternCtx,
    *,
    step: int,
    txn_type: str,
    amount: float,
    name_orig: str,
    dest: str,
    category: str,
    subcategory: str,
    merchant: str,
    region: str | None = None,
    account_type: str = "checking",
    is_fraud: int = 0,
    is_anomaly: int = 0,
    anomaly_type: str = "",
    archetype: str = "",
    ratio: float | None = None,
    pair_id: tuple | None = None,
    victim: str | None = None,
) -> dict:
    row = {
        "step": int(step),
        "type": txn_type,
        "amount": round2(amount),
        "nameOrig": name_orig,
        "nameDest": dest,
        "merchant": merchant,
        "category": category,
        "subcategory": subcategory,
        "transaction_region": region or ctx.persona.home_region,
        "account_type": account_type,
        "isFraud": int(is_fraud),
        "is_anomaly": int(is_anomaly),
        "anomaly_type": anomaly_type,
        "fraud_archetype": archetype,
        "label_reported_at_step": None,
    }
    if ratio is not None:
        row["_drain_ratio"] = float(ratio)
    if pair_id is not None:
        row["_pair_id"] = pair_id
    if victim is not None:
        row["_victim_id"] = victim
    return row


def _fraud(
    ctx: PatternCtx,
    *,
    step: int,
    txn_type: str,
    amount: float,
    name_orig: str,
    dest: str,
    category: str,
    subcategory: str,
    merchant: str,
    archetype: str,
    region: str | None = None,
    account_type: str = "checking",
    ratio: float | None = None,
    pair_id: tuple | None = None,
    victim: str | None = None,
) -> dict:
    """A labeled fraud row (isFraud=1, is_anomaly=1)."""
    return _row(
        ctx,
        step=step,
        txn_type=txn_type,
        amount=amount,
        name_orig=name_orig,
        dest=dest,
        category=category,
        subcategory=subcategory,
        merchant=merchant,
        region=region,
        account_type=account_type,
        is_fraud=1,
        is_anomaly=1,
        anomaly_type=archetype,
        archetype=archetype,
        ratio=ratio,
        pair_id=pair_id,
        victim=victim,
    )


def _hard_negative(
    ctx: PatternCtx,
    *,
    step: int,
    txn_type: str,
    amount: float,
    name_orig: str,
    dest: str,
    category: str,
    subcategory: str,
    merchant: str,
    archetype: str,
    region: str | None = None,
    account_type: str = "checking",
) -> dict:
    """A legitimate-but-unusual row: NOT fraud, NOT an injected anomaly."""
    return _row(
        ctx,
        step=step,
        txn_type=txn_type,
        amount=amount,
        name_orig=name_orig,
        dest=dest,
        category=category,
        subcategory=subcategory,
        merchant=merchant,
        region=region,
        account_type=account_type,
        is_fraud=0,
        is_anomaly=0,
        archetype=archetype,
    )


# ---------------------------------------------------------------- easy tier
