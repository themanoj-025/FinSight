"""Hypothesis property-based tests for the generator invariants (Phase F.2).

The example-based tests (test_generate_data.py, test_data_realism.py) pin
specific ``(seed, days, n_background)`` combinations. These property tests
generalize the generator's core invariants — schema stability, per-account
balance continuity, non-negative clamped balances, and the fraud-rate upper
band — to *any* valid parameter combination in a modest range, so a regression
that only shows up off the fixture points gets caught rather than silently
slipping through the specific windows the example tests exercise.

Generation cost is kept deliberately low (tiny tier, <= 120 days, single
focal user) so the whole module stays inside the fast-suite budget: each
example generates one ledger and asserts every invariant against it.
"""

from __future__ import annotations

import numpy as np
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from finance_agent import datagen

# Modest search space: tiny tier keeps each generation fast while still
# exercising different fraud densities, account-pool sizes, and window
# lengths (incl. multi-month windows where seasonality shifts).
_given = st.fixed_dictionaries(
    {
        "seed": st.integers(min_value=0, max_value=999),
        "days": st.integers(min_value=30, max_value=120),
        "n_bg": st.integers(min_value=2, max_value=120),
    }
)

# The fraud-rate BAND is a statistical claim that needs enough rows for
# single-event granularity to wash out (a 7-row ledger with one fraud event is
# ~14% — structurally over any band no matter what the generator does). The 6%
# bound below is grounded in a measured sweep of THIS generator on the tiny
# tier (Python 3.14.5, the environment where this test previously flaked):
#
#   n_bg  days   max      p99      seeds >6%
#   ----  -----  -------  -------  --------
#   20    30     8.78%    7.33%    24/200     <- too small: variance breaks any tight band
#   30    30     5.78%    5.44%     0/200
#   50    30     3.79%    3.50%     0/50
#   50    120    1.40%    1.34%     0/50
#   120   30     1.71%    1.53%     0/50
#   120   120    0.60%    0.60%     0/50
##   n_bg=20 is the only corner that exceeds 6% (single-event granularity: a
# handful of injected fraud rows over a small pool moves the realized rate by
# whole percentage points). n_bg>=30 has never exceeded 6% in 250 sampled
# seeds — the audit's failing combo (seed=454, days=30, n_bg=20 -> 6.33%) drops
# to 4.17% at n_bg=30. So the pool floor is set at 30: the 6% band stays tight
# enough to catch a genuine rate regression at every pool size it now covers,
# and the genuinely-noisy tiny-pool corner (n_bg < 30) is tested separately
# below with an explicitly wide tolerance instead of being papered over. The
# worst measured margin at the new floor is ~0.22pp (n_bg=30, days=30 max
# 5.78% vs the 6% band over 200 seeds) — documented here so the floor is never
# mistaken for a round number; the tiny-pool test below closes the gap for
# n_bg in [2, 29].
_given_pool = st.fixed_dictionaries(
    {
        "seed": st.integers(min_value=0, max_value=999),
        "days": st.integers(min_value=30, max_value=120),
        "n_bg": st.integers(min_value=30, max_value=120),
    }
)

# The tiny-pool corner (n_bg < 30) is statistically noisy by construction: one
# injected fraud event over a small ledger can be several percent of all rows.
# Measured max over 100 seeds (Python 3.14.5): n_bg=2 days=30 -> 25.5%,
# n_bg=5 days=30 -> 18.2%, n_bg=10 days=30 -> 10.6%, n_bg=15 -> 8.3%, and
# days=120 is calmer at every pool size (single-event granularity weakens as
# the row count grows). No tight band is meaningful here, so this property
# only asserts a sanity-level ceiling (30%) — it catches a generator that
# starts flooding the ledger with fraud, while never flaking on legitimate
# small-sample variance. (Structural correctness for tiny pools is already
# pinned by `test_generator_invariants_hold_for_any_valid_params`, which
# covers n_bg >= 2.) The range extends to n_bg=29 so every pool size is
# covered by exactly one rate property: the tight-band test above owns
# n_bg >= 30, this one owns n_bg < 30.
_given_tiny_pool = st.fixed_dictionaries(
    {
        "seed": st.integers(min_value=0, max_value=999),
        "days": st.integers(min_value=30, max_value=120),
        "n_bg": st.integers(min_value=2, max_value=29),
    }
)

# Core schema every tier must carry (the columns features/tools consume).
# Verified additive columns (fraud_archetype, is_anomaly, ...) included so a
# generator refactor that drops a downstream column fails here first.
_REQUIRED_COLUMNS = {
    "step",
    "nameOrig",
    "type",
    "amount",
    "oldbalanceOrg",
    "newbalanceOrig",
    "isFraud",
    "is_focal_user",
    "fraud_archetype",
    "is_anomaly",
    "account_type",
    "category",
    "date",
}


@settings(max_examples=20, deadline=30_000, suppress_health_check=(HealthCheck.too_slow,))
@given(_given)
def test_generator_invariants_hold_for_any_valid_params(params: dict) -> None:
    """Schema, balance continuity, non-negative balances hold for any
    (seed, days, n_background_accounts) in the search space."""
    df = datagen.generate_dataset(
        days=int(params["days"]),
        seed=int(params["seed"]),
        tier="tiny",
        users=["U_Alex"],
        n_background_accounts=int(params["n_bg"]),
    )

    # -- schema stability ---------------------------------------------------
    missing = _REQUIRED_COLUMNS - set(df.columns)
    assert not missing, f"missing columns for params={params}: {sorted(missing)}"
    assert len(df) > 0, f"empty ledger for params={params}"

    focal = df[df["is_focal_user"]].sort_values("step", kind="stable")
    assert not focal.empty, f"no focal rows for params={params}"

    # -- per-row balance arithmetic + clamped non-negativity ----------------
    credit = focal["type"].isin(("SALARY", "CASH_IN"))
    expected_new = focal["oldbalanceOrg"] + focal["amount"] * np.where(credit, 1.0, -1.0)
    assert np.isclose(
        focal["newbalanceOrig"].to_numpy(), expected_new.to_numpy(), atol=0.02
    ).all(), f"balance arithmetic broken for params={params}"
    assert (focal["newbalanceOrig"] >= -0.02).all(), (
        f"clamped balance went negative for params={params}"
    )

    # -- per-account old -> new chaining ------------------------------------
    for _, g in focal.groupby("nameOrig", sort=False):
        if len(g) > 1:
            assert np.isclose(
                g["oldbalanceOrg"].iloc[1:].to_numpy(),
                g["newbalanceOrig"].iloc[:-1].to_numpy(),
                atol=0.02,
            ).all(), f"balance chain broken for {g['nameOrig'].iloc[0]} (params={params})"

    assert df["isFraud"].isin((0, 1)).all(), "isFraud must be binary"


@settings(max_examples=20, deadline=30_000, suppress_health_check=(HealthCheck.too_slow,))
@given(_given_pool)
def test_fraud_rate_upper_band_holds_for_meaningful_pools(params: dict) -> None:
    """The defensible fraud-rate band holds once the pool is large enough for
    single-event granularity to wash out (pool sizes >= the tiny-tier default)."""
    df = datagen.generate_dataset(
        days=int(params["days"]),
        seed=int(params["seed"]),
        tier="tiny",
        users=["U_Alex"],
        n_background_accounts=int(params["n_bg"]),
    )
    rate = float(df["isFraud"].mean())
    assert 0.0 <= rate <= 0.06, f"fraud rate {rate:.5f} out of band (params={params})"
    assert df["isFraud"].isin((0, 1)).all(), "isFraud must be binary"


@settings(max_examples=20, deadline=30_000, suppress_health_check=(HealthCheck.too_slow,))
@given(_given_tiny_pool)
def test_fraud_rate_stays_bounded_for_noisy_tiny_pools(params: dict) -> None:
    """Tiny pools (n_bg < 30) get a deliberately wide, sanity-level ceiling.

    Small ledgers are single-event-noise dominated: the rate band that holds
    for meaningful pools (above) is not a statistical claim about this corner,
    so this property only asserts the generator never floods the ledger with
    fraud (30% ceiling) — see the `_given_tiny_pool` comment for the measured
    variance that justifies the split."""
    df = datagen.generate_dataset(
        days=int(params["days"]),
        seed=int(params["seed"]),
        tier="tiny",
        users=["U_Alex"],
        n_background_accounts=int(params["n_bg"]),
    )
    rate = float(df["isFraud"].mean())
    assert 0.0 <= rate <= 0.30, f"fraud rate {rate:.5f} out of noisy-pool ceiling (params={params})"


@settings(max_examples=10, deadline=30_000, suppress_health_check=(HealthCheck.too_slow,))
@given(_given)
def test_fraud_labels_carry_archetype_metadata(params: dict) -> None:
    """When fraud fires, the rows are labeled with a fraud archetype (never a
    silent label-less flag) — the property the rules/blend UX depends on."""
    df = datagen.generate_dataset(
        days=int(params["days"]),
        seed=int(params["seed"]),
        tier="tiny",
        users=["U_Alex"],
        n_background_accounts=int(params["n_bg"]),
    )
    fraud = df[df["isFraud"] == 1]
    if fraud.empty:
        return  # short windows legitimately produce no fraud — upper band is tested above
    assert fraud["fraud_archetype"].notna().all(), (
        f"fraud rows without an archetype label (params={params})"
    )
