"""Fraud pattern library barrel — public surface for the data generators.

Compatibility shim kept after the split into ``finance_agent/patterns_pkg``:
the data-gen modules and the test suite import these names via
``from finance_agent import fraud_patterns as fp``. This re-export is
load-bearing — do not remove (an import-pruning sweep previously gutted this
file and broke ``generate_data.py`` at runtime).
"""

from __future__ import annotations

from finance_agent.patterns_pkg.ctx import (
    EASY_PATTERNS,
    HARD_NEGATIVES,
    HARD_PATTERNS,
    MEDIUM_PATTERNS,
    PATTERN_RATES,
    PatternCtx,
    _MIN_DAYS,
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
from finance_agent.patterns_pkg.registry import (
    apply_discovery_lag,
    gen_pattern,
    inject_background_patterns,
    inject_focal_patterns,
    pattern_names,
)

__all__ = [
    "EASY_PATTERNS",
    "HARD_NEGATIVES",
    "HARD_PATTERNS",
    "MEDIUM_PATTERNS",
    "PATTERN_RATES",
    "PatternCtx",
    "_MIN_DAYS",
    "apply_discovery_lag",
    "gen_account_takeover",
    "gen_balance_drain",
    "gen_bust_out",
    "gen_card_testing",
    "gen_duplicate_charge",
    "gen_life_event",
    "gen_mimicry",
    "gen_new_payee_transfer",
    "gen_pattern",
    "gen_rapid_burst",
    "gen_refund_abuse",
    "gen_seasonal_mimicry",
    "gen_slow_balance_drain",
    "gen_spend_spike",
    "gen_subscription_creep",
    "gen_travel",
    "inject_background_patterns",
    "inject_focal_patterns",
    "pattern_names",
]
