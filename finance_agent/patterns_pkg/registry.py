"""Pattern registry — difficulty tables, dispatch, and injection helpers.

The registry tables (rates, minimum-window sizes, generator dispatch) moved
here from the original ``fraud_patterns.py`` monolith. The tier lists and
``_MIN_DAYS`` live in :mod:`finance_agent.patterns_pkg.ctx` next to the
pattern definitions; the per-pattern rates belong to the registry.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from finance_agent.patterns_pkg.ctx import (
    _MIN_DAYS,
    EASY_PATTERNS,
    HARD_NEGATIVES,
    HARD_PATTERNS,
    MEDIUM_PATTERNS,
    PATTERN_RATES,
    PatternCtx,
)
from finance_agent.patterns_pkg.generators import (
    gen_balance_drain,
    gen_card_testing,
    gen_duplicate_charge,
    gen_new_payee_transfer,
    gen_refund_abuse,
    gen_slow_balance_drain,
    gen_spend_spike,
    gen_subscription_creep,
)
from finance_agent.patterns_pkg.generators_advanced import (
    gen_account_takeover,
    gen_bust_out,
    gen_life_event,
    gen_mimicry,
    gen_rapid_burst,
    gen_seasonal_mimicry,
    gen_travel,
)

_PATTERN_FNS: dict[str, Callable[..., list[dict]]] = {
    "balance_drain": gen_balance_drain,
    "duplicate_charge": gen_duplicate_charge,
    "spend_spike": gen_spend_spike,
    "card_testing": gen_card_testing,
    "slow_balance_drain": gen_slow_balance_drain,
    "new_payee_transfer": gen_new_payee_transfer,
    "subscription_creep": gen_subscription_creep,
    "refund_abuse": gen_refund_abuse,
    "mimicry": gen_mimicry,
    "account_takeover": gen_account_takeover,
    "bust_out": gen_bust_out,
    "seasonal_mimicry": gen_seasonal_mimicry,
    "life_event": gen_life_event,
    "travel": gen_travel,
    "rapid_burst": gen_rapid_burst,
}


def pattern_names() -> list[str]:
    return list(_PATTERN_FNS)


def gen_pattern(name: str, ctx: PatternCtx, day: int | None = None) -> list[dict]:
    fn = _PATTERN_FNS[name]
    return fn(ctx, day)


def inject_focal_patterns(ctx: PatternCtx, *, force_easy: bool = True) -> list[dict]:
    """Sample which patterns fire for a focal persona and generate their rows.

    ``force_easy`` guarantees the deterministic easy tier (drain/dup/spike)
    whenever the window allows, so tiny-tier tests keep seeing them.
    Injection is scoped to ``ctx.day_lo..ctx.day_hi`` (one call per year for
    multi-year tiers), and medium/hard rates scale with ``ctx.scale``.
    """
    rows: list[dict] = []
    for name in EASY_PATTERNS + MEDIUM_PATTERNS + HARD_PATTERNS + HARD_NEGATIVES:
        if name in EASY_PATTERNS:
            fires = force_easy or ctx.rng.random() < PATTERN_RATES[name] * ctx.scale
        else:
            fires = ctx.rng.random() < PATTERN_RATES[name] * ctx.scale
        if fires and ctx.window_days >= _MIN_DAYS[name]:
            rows += gen_pattern(name, ctx)
    return rows


def inject_background_patterns(ctx: PatternCtx, *, bust: bool = False) -> list[dict]:
    """Fraud for a background account: optional bust-out."""
    rows: list[dict] = []
    if bust and ctx.window_days >= _MIN_DAYS["bust_out"]:
        rows += gen_bust_out(ctx)
    return rows


def apply_discovery_lag(
    rows: list[dict], rng: np.random.Generator, lag_rate: float = 0.02
) -> list[dict]:
    """Label realism: ~`lag_rate` of fraud rows are only *knowable* later.

    ``label_reported_at_step`` > ``step`` mimics chargeback-reporting delay —
    the label exists in the final snapshot, but a streaming evaluator that
    only trusts labels reported up to time *t* would not see it yet.
    """
    for row in rows:
        if row["isFraud"] and rng.random() < lag_rate:
            lag_hours = int(rng.integers(24, 30 * 24))
            row["label_reported_at_step"] = int(row["step"]) + lag_hours
        else:
            row["label_reported_at_step"] = int(row["step"])
    return rows
