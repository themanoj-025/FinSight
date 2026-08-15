"""Shared constants and formatting helpers.

Everything in this module is import-only — it must never import from the rest
of the package, so any module can depend on it without creating a cycle.
"""

from __future__ import annotations

from typing import Any

SPENDING_CATEGORIES = [
    "groceries",
    "dining",
    "transport",
    "utilities",
    "entertainment",
    "shopping",
    "health",
    "subscriptions",
]

# Transaction types that credit the account (income), never spending.
CREDIT_TYPES = {"SALARY", "CASH_IN"}

# The full, canonical set of transaction types the generator can emit, in a
# stable order. Type one-hot columns are reindexed against this list so a
# train/test split where one side lacks a rare type still produces identical
# feature columns (keep in sync with generate_data.py).
TRANSACTION_TYPES = [
    "CASH_IN",
    "CASH_OUT",
    "DEBIT",
    "PAYMENT",
    "SALARY",
    "SHOP",
    "SUBSCRIPTION",
    "TRANSFER",
]

# Canonical account types (multi-account generator, finance_agent/datagen.py).
# Feature account-type one-hot columns are reindexed against this list so a
# split that lacks a rare account type still emits the exact same column set.
ACCOUNT_TYPES = ["checking", "savings", "credit", "background"]

# The 15 difficulty-graded fraud/anomaly archetypes (finance_agent/fraud_patterns.py)
# plus the hard-negative slugs — used by the benchmark's per-archetype recall.
FRAUD_ARCHETYPES = [
    "balance_drain",
    "duplicate_charge",
    "spend_spike",
    "card_testing",
    "slow_balance_drain",
    "new_payee_transfer",
    "subscription_creep",
    "refund_abuse",
    "mimicry",
    "account_takeover",
    "bust_out",
    "seasonal_mimicry",
]
HARD_NEGATIVE_ARCHETYPES = [
    "hard_negative_life_event",
    "hard_negative_travel",
    "hard_negative_rapid_burst",
]

# Model IDs the Settings page is allowed to write into config.yaml. This is an
# allowlist on purpose: arbitrary free-text must never reach yaml.safe_dump.
MODEL_ALLOWLIST = [
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-0",
    "claude-opus-4-1",
    "claude-opus-4-1-20250805",
    "claude-3-5-haiku-latest",
    "claude-3-5-haiku-20241022",
    "claude-3-haiku-20240307",
]

DEFAULT_MODEL = "claude-sonnet-4-5"


def fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def fmt(value: Any) -> str:
    return str(value)
