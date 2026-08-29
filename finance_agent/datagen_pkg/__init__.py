"""Datagen package — vectorized synthetic-data generator.

Re-exports all public names for backward compatibility::

    from finance_agent import datagen
    datagen.generate_dataset(...)
"""

from finance_agent.datagen_pkg.background import _background_ledger, _bg_one
from finance_agent.datagen_pkg.balance import (
    _balance_one,
    _bust_persona,
    _clamped_cumsum,
    _resolve_drains,
)
from finance_agent.datagen_pkg.config import (
    _CASH_ACCOUNT,
    _INFLATION,
    FINAL_COLUMNS,
    FOCAL_NAMES,
    LEGACY_COLUMNS,
    MERCHANTS_INDEX,
    NEW_COLUMNS,
    PARTIAL_COLUMNS,
    SUB_SUBCATEGORY,
    TIER_DEFAULTS,
)
from finance_agent.datagen_pkg.generate import (
    generate_dataset,
    persona_manifest,
    tier_stats,
)
from finance_agent.datagen_pkg.helpers import (
    _CLUSTER,
    _PERIODS,
    _days_since_payday,
    _frame,
    _payday_mask,
    _pick_merchant,
    _raise_multipliers,
    focal_user_ids,
)
from finance_agent.datagen_pkg.persona_ledger import _persona_ledger
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

__all__ = [
    # Public API
    "generate_dataset",
    "persona_manifest",
    "tier_stats",
    "focal_user_ids",
    # Config
    "FOCAL_NAMES",
    "FINAL_COLUMNS",
    "LEGACY_COLUMNS",
    "MERCHANTS_INDEX",
    "NEW_COLUMNS",
    "PARTIAL_COLUMNS",
    "SUB_SUBCATEGORY",
    "TIER_DEFAULTS",
    "_CASH_ACCOUNT",
    "_INFLATION",
    # Helpers
    "_CLUSTER",
    "_PERIODS",
    "_days_since_payday",
    "_frame",
    "_payday_mask",
    "_pick_merchant",
    "_raise_multipliers",
    # Persona ledger
    "_persona_ledger",
    # Transactions
    "_big_txns",
    "_credit_autopay",
    "_discretionary",
    "_inject_patterns",
    "_regular_trips",
    "_salaried_cashin",
    "_savings_transfer",
    "_spend_frame",
    # Background
    "_bg_one",
    "_background_ledger",
    # Balance
    "_balance_one",
    "_bust_persona",
    "_clamped_cumsum",
    "_resolve_drains",
]
