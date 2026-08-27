"""Datagen — Utility helpers (ID generation, merchant selection, frame builder, payday math)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from finance_agent.datagen_pkg.config import (
    FOCAL_NAMES,
    MERCHANTS_INDEX,
    PARTIAL_COLUMNS,
)

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
