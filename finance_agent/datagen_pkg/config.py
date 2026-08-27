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
