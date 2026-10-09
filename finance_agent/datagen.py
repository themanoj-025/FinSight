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

.. note::

   The implementation has been refactored into ``finance_agent.datagen_pkg``
   for maintainability.  This module re-exports every public and private name
   so that existing ``from finance_agent import datagen`` imports continue to
   work unchanged.
"""

from __future__ import annotations

# Re-export everything from the datagen_pkg package for backward compatibility.
from finance_agent.datagen_pkg import (
    _CASH_ACCOUNT,
    _CLUSTER,
    _INFLATION,
    _PERIODS,
    FINAL_COLUMNS,
    FOCAL_NAMES,
    LEGACY_COLUMNS,
    MERCHANTS_INDEX,
    NEW_COLUMNS,
    PARTIAL_COLUMNS,
    SUB_SUBCATEGORY,
    TIER_DEFAULTS,
    _balance_one,
    _bg_one,
    _big_txns,
    _bust_persona,
    _clamped_cumsum,
    _credit_autopay,
    _days_since_payday,
    _discretionary,
    _frame,
    _inject_patterns,
    _payday_mask,
    _persona_ledger,
    _pick_merchant,
    _raise_multipliers,
    _regular_trips,
    _resolve_drains,
    _salaried_cashin,
    _savings_transfer,
    _spend_frame,
    focal_user_ids,
    generate_dataset,
    persona_manifest,
    tier_stats,
)

__all__ = [
    "FINAL_COLUMNS",
    "FOCAL_NAMES",
    "LEGACY_COLUMNS",
    "MERCHANTS_INDEX",
    "NEW_COLUMNS",
    "PARTIAL_COLUMNS",
    "SUB_SUBCATEGORY",
    "TIER_DEFAULTS",
    "_CASH_ACCOUNT",
    "_CLUSTER",
    "_INFLATION",
    "_PERIODS",
    "_balance_one",
    "_bg_one",
    "_big_txns",
    "_bust_persona",
    "_clamped_cumsum",
    "_credit_autopay",
    "_days_since_payday",
    "_discretionary",
    "_frame",
    "_inject_patterns",
    "_payday_mask",
    "_persona_ledger",
    "_pick_merchant",
    "_raise_multipliers",
    "_regular_trips",
    "_resolve_drains",
    "_salaried_cashin",
    "_savings_transfer",
    "_spend_frame",
    "focal_user_ids",
    "generate_dataset",
    "persona_manifest",
    "tier_stats",
]
