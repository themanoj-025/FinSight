"""Transaction-level feature engineering, shared by model training and inference.

Every feature here is deterministic and **strictly backward-looking**: a feature
for row *i* only ever depends on rows with ``step <= row_i.step``. Velocity
features (cumulative counts, trailing windows) already were; ``amount_vs_*``
aggregates previously used full-frame means, which leaked future information
into a row. They now use a trailing per-category mean instead, so a temporal
train/test split is clean without storing fitted statistics anywhere.

The data-gen v2 columns add **causal** features (Data-Gen §6): the fraud
patterns are built around behavioural signals (first-time payees, out-of-home
regions, unusual account channels), and the feature set mirrors them so the
model can actually learn the same structure the generator used to hide it:

  * ``region_distance_miles`` / ``is_out_of_home_region`` — distance between
    the transaction region and the persona's home region (patterns 6/10/14);
  * ``is_new_merchant`` / ``is_new_payee`` — first sighting of a merchant or
    destination for this account (patterns 2/6/7/8);
  * ``is_credit_account`` / ``is_savings_account`` — which of the persona's
    accounts the money moved through (multi-account model);
  * ``is_weekend`` — calendar context (weekend burst spending, pattern 15).

Each is computed from columns that exist in the final generated snapshot, so
applying ``build_features`` to any future ledger (or a legacy one missing the
v2 columns — those features degrade to neutral 0.0) is safe.

Because of this property, ``build_features`` can be applied identically to a
training frame and to any new transactions the agent wants to score — no fitted
transformers to carry across the split.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from finance_agent.constants import ACCOUNT_TYPES, TRANSACTION_TYPES
from finance_agent.merchants import REGION_IDS, haversine_miles

# Precomputed pairwise great-circle distances for every region pair, as both a
# dict (legacy lookups) and a (|REGION_IDS| x |REGION_IDS|) matrix so the
# per-row feature is a vectorized fancy index, not a per-row trig computation or
# a 10M-row Python loop (bench tier).
_REGION_POS = {r: i for i, r in enumerate(REGION_IDS)}
_REGION_DIST: dict[tuple[str, str], float] = {
    (a, b): haversine_miles(a, b) for a in REGION_IDS for b in REGION_IDS
}
_REGION_DIST_MAT: np.ndarray = np.asarray(
    [[haversine_miles(a, b) for b in REGION_IDS] for a in REGION_IDS], dtype=float
)


def _first_seen_mask(grouped: pd.core.groupby.generic.DataFrameGroupBy, col: str) -> np.ndarray:
    """1.0 for the first occurrence of `col` within each group, else 0.0."""
    return grouped[col].cumcount().to_numpy().astype(float) == 0.0


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Return a feature matrix (same row order as `df`), one row per transaction.

    Features fall into four groups:
      1. raw + transformed value features (amount, balances, ratios)
      2. account-level velocity features (prior activity from the same account)
      3. context features (hour of day, trailing category-relative amount, type one-hot)
      4. causal v2 features (region distance, new merchant/payee, account channel)
    """
    # Stable sort: pandas 2.x's default quicksort is *unstable*, so rows with
    # the same step can reorder between calls (and between full-frame vs
    # prefix builds), breaking row-position assumptions the leakage tests
    # assert. pandas 3.x already defaults to stable — this pins the behavior
    # on every supported pandas version.
    d = df.sort_values("step", kind="stable").reset_index(drop=True)
    f = pd.DataFrame(index=d.index)

    # --- 1. Raw / transformed value features -------------------------------
    f["amount"] = d["amount"]
    f["log_amount"] = np.log1p(d["amount"])
    f["amount_ratio_orig"] = d["amount"] / (d["oldbalanceOrg"] + 1.0)
    f["amount_ratio_dest"] = np.where(
        d["oldbalanceDest"] > 0, d["amount"] / (d["oldbalanceDest"] + 1.0), 0.0
    )
    f["balance_after_ratio"] = d["newbalanceOrig"] / (d["oldbalanceOrg"] + 1.0)
    # Credit accounts carry negative (debt) balances, and autopay transactions
    # briefly debit the card as well — log1p of a balance below -1 would be NaN.
    # Clamp at 0: outstanding liability is not a usable balance for these ratio
    # features, and the model must never see a missing value in a fold.
    f["log_oldbalance_orig"] = np.log1p(np.maximum(d["oldbalanceOrg"], 0.0))
    f["log_oldbalance_dest"] = np.log1p(np.maximum(d["oldbalanceDest"], 0.0))
    f["is_focal"] = d["is_focal_user"].astype(float)

    # --- 2. Velocity features per originating account ----------------------
    by_orig = d.groupby("nameOrig", sort=False)
    f["count_prior_orig"] = by_orig.cumcount().astype(float)
    f["sum_amount_prev7_orig"] = (
        by_orig["amount"]
        .transform(lambda s: s.rolling(7, min_periods=1).sum().shift(1))
        .fillna(0.0)
    )
    f["steps_since_prev_orig"] = by_orig["step"].transform(lambda s: s.diff()).fillna(999.0)

    by_dest = d.groupby("nameDest", sort=False)
    f["count_prior_dest"] = by_dest.cumcount().astype(float)
    f["sum_amount_prev7_dest"] = (
        by_dest["amount"]
        .transform(lambda s: s.rolling(7, min_periods=1).sum().shift(1))
        .fillna(0.0)
    )

    # --- 3. Context features -----------------------------------------------
    f["hour_of_day"] = d["step"] % 24
    # Trailing per-category mean (previous occurrences of this category only),
    # so no row references future information. First occurrence of a category
    # falls back to its own amount, giving a neutral ratio ~= 1.
    cat_mean = d.groupby("category", sort=False)["amount"].transform(
        lambda s: s.expanding(min_periods=1).mean().shift(1)
    )
    f["amount_vs_category_mean"] = d["amount"] / (cat_mean.fillna(d["amount"]) + 1.0)

    # --- 4. Causal v2 features (degrade to neutral 0.0 on legacy data) -----
    has_region = "transaction_region" in d.columns and "home_region" in d.columns
    if has_region:
        tx_reg = d["transaction_region"].astype(str).to_numpy()
        home_reg = d["home_region"].astype(str).to_numpy()
        # Vectorized matrix lookup: region codes -> 2D index into the
        # precomputed distance matrix (unknown/legacy region -> distance 0.0,
        # matching the old per-pair dict behaviour).
        codes_tx = pd.Categorical(tx_reg, categories=REGION_IDS).codes
        codes_home = pd.Categorical(home_reg, categories=REGION_IDS).codes
        valid = (codes_tx >= 0) & (codes_home >= 0)
        dist = np.zeros(len(tx_reg), dtype=float)
        dist[valid] = _REGION_DIST_MAT[codes_tx[valid], codes_home[valid]]
        f["region_distance_miles"] = dist
        f["is_out_of_home_region"] = (tx_reg != home_reg).astype(float)
    else:
        f["region_distance_miles"] = 0.0
        f["is_out_of_home_region"] = 0.0

    if "merchant" in d.columns and "nameOrig" in d.columns:
        f["is_new_merchant"] = _first_seen_mask(
            d.groupby(["nameOrig", "merchant"], sort=False), "merchant"
        ).astype(float)
    else:
        f["is_new_merchant"] = 0.0
    if "nameDest" in d.columns and "nameOrig" in d.columns:
        f["is_new_payee"] = _first_seen_mask(
            d.groupby(["nameOrig", "nameDest"], sort=False), "nameDest"
        ).astype(float)
    else:
        f["is_new_payee"] = 0.0

    if "account_type" in d.columns:
        # One-hot against the canonical account-type list so a split that lacks
        # a rare account type still emits the exact same column set.
        acct_dummies = (
            pd.get_dummies(d["account_type"], prefix="account")
            .astype(float)
            .reindex(columns=[f"account_{t}" for t in ACCOUNT_TYPES], fill_value=0.0)
        )
        f = pd.concat([f, acct_dummies], axis=1)
    else:
        # Legacy data: every row is the primary checking account.
        f["account_checking"] = 1.0
        f["account_savings"] = 0.0
        f["account_credit"] = 0.0
        f["account_background"] = 0.0

    # Calendar context: weekend = Saturday/Sunday from the ISO datetime/date.
    if "datetime" in d.columns:
        try:
            dt = pd.to_datetime(d["datetime"])
            f["is_weekend"] = (dt.dt.weekday >= 5).astype(float)
        except (TypeError, ValueError):
            f["is_weekend"] = 0.0
    elif "date" in d.columns:
        try:
            dt = pd.to_datetime(d["date"])
            f["is_weekend"] = (dt.dt.weekday >= 5).astype(float)
        except (TypeError, ValueError):
            f["is_weekend"] = 0.0
    else:
        f["is_weekend"] = 0.0

    # Reindex the one-hot columns against the canonical type list so a split that
    # lacks a rare type still emits the exact same column set.
    type_dummies = (
        pd.get_dummies(d["type"], prefix="type")
        .astype(float)
        .reindex(columns=[f"type_{t}" for t in TRANSACTION_TYPES], fill_value=0.0)
    )
    f = pd.concat([f, type_dummies], axis=1)

    return f.astype(float)
