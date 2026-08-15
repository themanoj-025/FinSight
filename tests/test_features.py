"""Feature-engineering tests (Phase 3.1) — including the temporal-leakage guard.

`build_features` must be strictly backward-looking: a row's features may only
depend on rows at or before its own `step`. `test_no_temporal_leakage` enforces
this by removing future rows and asserting the row's features don't change.
"""

import numpy as np
import pandas as pd
import pytest

from finance_agent import features
from finance_agent.constants import ACCOUNT_TYPES, TRANSACTION_TYPES
from generate_data import generate

# 15 value/velocity/context features + 9 causal v2 features (region distance,
# out-of-home, new merchant/payee, 4 account-channel one-hots, weekend) + one
# column per canonical transaction type.
CAUSAL_V2_FEATURES = [
    "region_distance_miles",
    "is_out_of_home_region",
    "is_new_merchant",
    "is_new_payee",
    "is_weekend",
]
EXPECTED_FEATURE_COUNT = 15 + len(CAUSAL_V2_FEATURES) + len(ACCOUNT_TYPES) + len(TRANSACTION_TYPES)


@pytest.fixture(scope="module")
def df() -> pd.DataFrame:
    return (
        generate(
            days=30,
            seed=3,
            user="U_Alex",
            n_background_accounts=15,
            n_fraud_pairs=2,
            start_date="2025-01-01",
        )
        .sort_values("step")
        .reset_index(drop=True)
    )


def test_build_features_shape_and_columns(df):
    f = features.build_features(df)
    assert len(f) == len(df)
    assert f.shape[1] == EXPECTED_FEATURE_COUNT
    # every canonical type has a dummy column
    for t in TRANSACTION_TYPES:
        assert f"type_{t}" in f.columns
    # causal v2 features + account-channel one-hots are present
    for col in CAUSAL_V2_FEATURES + [f"account_{t}" for t in ACCOUNT_TYPES]:
        assert col in f.columns
    assert f.index.name is None or f.index.name == "index"


def test_causal_v2_features_are_sensible(df):
    f = features.build_features(df)
    # out-of-home rows exist (travel patterns) and their distance is > 0
    assert f["is_out_of_home_region"].between(0, 1).all()
    assert f["region_distance_miles"].min() >= 0.0
    assert (f.loc[f["is_out_of_home_region"] == 1, "region_distance_miles"] > 0).all()
    # new merchant/payee are 0/1 flags; some first-seen rows exist
    assert f["is_new_merchant"].isin([0.0, 1.0]).all()
    assert f["is_new_payee"].isin([0.0, 1.0]).all()
    assert f["is_new_payee"].sum() > 0
    assert f["is_weekend"].isin([0.0, 1.0]).all()
    # account one-hots are consistent (each row belongs to exactly one channel)
    acct_cols = [f"account_{t}" for t in ACCOUNT_TYPES]
    assert (f[acct_cols].sum(axis=1) == 1.0).all()


def test_out_of_home_region_flag(df):
    """Data-Gen §4: the region signal is a per-row, causal flag.

    ``is_out_of_home_region`` is 1 only when the transaction region differs
    from the persona's home region (with a > 0 great-circle distance); every
    home-region transaction carries 0 and zero distance.
    """
    f = features.build_features(df)
    d = df.sort_values("step").reset_index(drop=True)
    home = (d["transaction_region"] == d["home_region"]).to_numpy()
    away = ~home
    assert home.any() and away.any(), "both home and away transactions must exist"
    assert (f.loc[home, "is_out_of_home_region"] == 0.0).all()
    assert (f.loc[home, "region_distance_miles"] == 0.0).all()
    assert (f.loc[away, "is_out_of_home_region"] == 1.0).all()
    assert (f.loc[away, "region_distance_miles"] > 0.0).all()


def test_legacy_frame_without_v2_columns_still_builds(df):
    """A frame missing the v2 columns must build with neutral features."""
    legacy = df.drop(
        columns=[
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
    )
    f = features.build_features(legacy)
    assert list(f.columns) == list(features.build_features(df).columns)
    assert (f["region_distance_miles"] == 0.0).all()
    assert (f["account_checking"] == 1.0).all()


def test_account_type_dummies_stable_across_subset(df):
    """A frame missing a rare account type still emits the same column set."""
    full = features.build_features(df)
    if "account_type" in df.columns:
        subset_df = df[df["account_type"] != "credit"]
        subset = features.build_features(subset_df)
        assert list(full.columns) == list(subset.columns)


def test_no_nans_in_numeric_features(df):
    f = features.build_features(df)
    assert not f.isna().any().any(), f"NaNs in features:\n{f.isna().sum()[f.isna().sum() > 0]}"


def test_no_nans_with_negative_credit_balances():
    """Regression: credit accounts carry negative (debt) balances.

    An autopay TRANSFER briefly debits the card, so ``oldbalanceDest`` can be
    far below -1 — the log features must clamp at 0 instead of emitting NaN
    (which crashes LogisticRegression/StandardScaler at train time).
    """
    from generate_data import generate

    base = generate(days=20, seed=4, n_background_accounts=5)
    df = base.copy()
    df.loc[0, "oldbalanceDest"] = -2210.23
    df.loc[0, "newbalanceDest"] = -2793.88
    df.loc[0, "oldbalanceOrg"] = -350.0
    f = features.build_features(df)
    assert not f.isna().any().any()
    # debt clamps to a neutral zero-balance feature
    assert f["log_oldbalance_dest"].iloc[0] == 0.0
    assert f["log_oldbalance_orig"].iloc[0] == 0.0


def test_no_temporal_leakage(df):
    """A row's features must not change when future rows are removed."""
    full = features.build_features(df)
    probes = [0, len(df) // 3, len(df) // 2, len(df) - 1]
    for i in probes:
        step = df["step"].iloc[i]
        prefix = df[df["step"] <= step]
        partial = features.build_features(prefix)
        # df is sorted by step, so the prefix is df.iloc[:k]; row i keeps its position.
        pd.testing.assert_series_equal(
            full.iloc[i].astype(float),
            partial.iloc[i].astype(float),
            check_names=False,
        )


def test_category_dummies_stable_across_subset(df):
    """A frame missing a rare type still emits the exact same column set."""
    full = features.build_features(df)
    subset = features.build_features(df[df["type"] != "SALARY"])
    assert list(full.columns) == list(subset.columns)
    pd.testing.assert_frame_equal(full, features.build_features(df))


def test_velocity_features_are_backward_looking(df):
    f = features.build_features(df)
    # count_prior_orig counts earlier rows of the same account only — for the
    # first transaction of an account it must be 0.
    first_per_account = df.groupby("nameOrig", sort=False).head(1).index
    assert (f.loc[first_per_account, "count_prior_orig"] == 0.0).all()


def test_features_are_float64(df):
    f = features.build_features(df)
    assert (f.dtypes == np.float64).all()
