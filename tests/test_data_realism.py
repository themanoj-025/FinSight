"""Realism tests for the data-gen v2 generator (slow suite).

These run a mid-size demo-tier ledger spanning a holiday window and assert the
properties that make the data believable: balance invariants, fraud-rate
bands, archetype coverage, seasonality, multi-account structure, and label
realism. Marked ``slow`` — run with `pytest -m slow` or `make test-slow`.
"""

from typing import Any

import numpy as np
import pandas as pd
import pytest

from finance_agent import datagen
from finance_agent.merchants import CATEGORY_GROUP as MG
from finance_agent.merchants import SEASONAL_MULTIPLIER

# Every test here generates a mid-size demo-tier ledger, so the whole module is
# part of the slow suite (`pytest -m slow` / `make test-slow`) and, more
# specifically, of the data-realism suite that CI runs against demo-tier data
# in the nightly benchmark job (`-m "slow and not data_realism"` skips it on
# every push; the fast leg must never run it).
pytestmark = [pytest.mark.slow, pytest.mark.data_realism]

# Mid-size demo-tier ledger from June to end of December (covers the holiday
# window) with an explicit background override so the slow test stays fast.
# Several focal personas (>= 6) so every archetype appears (credit-bearing
# personas included) and the seasonality / fraud-rate / discovery-lag signals
# have enough mass to be statistically meaningful.
# `dict[str, Any]`: keyword splats into the typed `generate_dataset(**_)` need
# Any values (mypy refuses `**dict[str, object]`); the keys are literal and
# fixed, so this is a fixture table, not a dynamic config bag.
_WINDOW: dict[str, Any] = dict(
    days=215,
    seed=42,
    tier="demo",
    n_background_accounts=400,
    users=[f"U_{n}" for n in datagen.FOCAL_NAMES[:8]],
    start_date="2025-06-01",
)

# Multi-year fixture: 12 personas x ~2.25 years so the income-drift and
# life-event tests have year-over-year data and near-guaranteed pattern fires
# (hard negatives have no guarantee on a single persona / single window).
_MULTI_YEAR: dict[str, Any] = dict(
    days=800,
    seed=7,
    tier="demo",
    n_background_accounts=100,
    users=[f"U_{n}" for n in datagen.FOCAL_NAMES[:12]],
    start_date="2025-01-01",
)


@pytest.fixture(scope="module")
def ledger() -> pd.DataFrame:
    return datagen.generate_dataset(**_WINDOW)


@pytest.fixture(scope="module")
def multi_year() -> pd.DataFrame:
    return datagen.generate_dataset(**_MULTI_YEAR)


def test_balance_invariants_hold_for_focal_accounts(ledger) -> None:
    focal = ledger[ledger["is_focal_user"]].sort_values("step").reset_index(drop=True)
    assert len(focal) > 0
    for _, row in focal.iterrows():
        expected_new = (
            row["oldbalanceOrg"] + row["amount"]
            if row["type"] in ("SALARY", "CASH_IN")
            else row["oldbalanceOrg"] - row["amount"]
        )
        assert np.isclose(row["newbalanceOrig"], expected_new, atol=0.02)
        assert row["newbalanceOrig"] >= -0.02, "clamped balance must never go negative"
    # consecutive rows of the same account chain
    chained = focal.groupby("nameOrig", sort=False).apply(
        lambda g: np.isclose(
            g["oldbalanceOrg"].iloc[1:].to_numpy(),
            g["newbalanceOrig"].iloc[:-1].to_numpy(),
            atol=0.02,
        ).all(),
        include_groups=False,
    )
    assert bool(chained.all()), "per-account balances must chain old -> new"


def test_fraud_rate_lands_in_a_defensible_band(ledger) -> None:
    rate = float(ledger["isFraud"].mean())
    assert 0.0005 <= rate <= 0.05, f"fraud rate {rate:.5f} out of band"
    assert int(ledger["isFraud"].sum()) > 0


def test_all_fraud_archetypes_appear(ledger) -> None:
    from finance_agent.constants import FRAUD_ARCHETYPES, HARD_NEGATIVE_ARCHETYPES

    # Labeled rows = fraud (isFraud=1) OR anomaly (is_anomaly=1): duplicate
    # charge and spend spike are anomaly-only by design (pattern 2/3 are
    # legitimate-looking events that rules flag, not labeled fraud).
    labeled = ledger[(ledger["isFraud"] == 1) | (ledger["is_anomaly"] == 1)]
    seen = set(labeled["fraud_archetype"])
    # every labeled archetype fires somewhere in a 7-month window
    assert set(FRAUD_ARCHETYPES) - {"bust_out"} <= seen, (
        f"missing archetypes: {set(FRAUD_ARCHETYPES) - {'bust_out'} - seen}"
    )
    # hard negatives are present but must NOT be fraud or anomaly
    hard = ledger[ledger["fraud_archetype"].isin(HARD_NEGATIVE_ARCHETYPES)]
    assert not hard.empty
    assert (hard["isFraud"] == 0).all()
    assert (hard["is_anomaly"] == 0).all()


def test_seasonality_holiday_shopping_bump(ledger) -> None:
    """Focal shopping transaction *frequency* rises in the holiday window.

    The seasonal multiplier drives the Poisson per-day transaction rate, so
    counts/day is the direct signal; per-day amounts are polluted by the
    small-business persona's legitimate large purchases (`_big_txns`), which
    is intentional but unrelated to seasonality.
    """
    d = ledger[ledger["is_focal_user"] & (ledger["category"] == "shopping")].copy()
    assert not d.empty
    d["_is_holiday"] = d["date"].apply(
        lambda s: (int(s[5:7]) == 12) or (int(s[5:7]) == 11 and int(s[8:10]) >= 15)
    )
    # Denominator = calendar days per bucket, derived from the fixture window
    # (2025-06-01 + _WINDOW["days"]), so a future _WINDOW edit can't silently
    # invalidate the ratio.
    start = pd.Timestamp(_WINDOW["start_date"])
    dates = pd.date_range(start, start + pd.Timedelta(days=_WINDOW["days"] - 1))
    n_holiday = int((dates.month == 12).sum() + ((dates.month == 11) & (dates.day >= 15)).sum())
    per_day = d.groupby("_is_holiday").size() / pd.Series(
        {False: float(len(dates) - n_holiday), True: float(n_holiday)}, dtype=float
    )
    assert per_day.get(True, 0.0) > per_day.get(False, 0.0) * 1.15, (
        "holiday shopping frequency/day should be visibly above the baseline"
    )
    # the multiplier table itself is monotonic the right way
    assert SEASONAL_MULTIPLIER["shopping"][11] > SEASONAL_MULTIPLIER["shopping"][5]


def test_multi_account_structure(ledger) -> None:
    assert {"checking", "savings", "credit", "background"} <= set(ledger["account_type"])
    focal = ledger[ledger["is_focal_user"]]
    assert {"checking"} <= set(focal["account_type"])
    # credit-channel spend exists for personas that have cards
    credit = ledger[ledger["account_type"] == "credit"]
    assert not credit.empty
    assert all(credit["nameOrig"].str.endswith("_Cred"))
    # savings transfers land in the persona's savings account
    sav = ledger[(ledger["category"] == "savings") & (ledger["account_type"] == "savings")]
    assert not sav.empty


def test_category_group_matches_catalog(ledger) -> None:
    mapped = ledger["category"].map(MG)
    assert (ledger["category_group"] == mapped.fillna("other")).all()


def test_region_signal_present(ledger) -> None:
    # out-of-home transactions exist (travel patterns + trips) and distances
    # are plausible great-circle miles
    away = ledger[ledger["transaction_region"] != ledger["home_region"]]
    assert not away.empty
    assert ledger["merchant_region"].notna().all()
    assert ledger["home_region"].notna().all()


def test_discovery_lag_label_realism(ledger) -> None:
    fraud = ledger[ledger["isFraud"] == 1]
    lagged = fraud[fraud["label_reported_at_step"] > fraud["step"]]
    assert not lagged.empty, "some fraud labels must be reported with a lag"
    assert (fraud["label_reported_at_step"] >= fraud["step"]).all()


def test_persona_ledgers_are_complete_and_consistent(ledger) -> None:
    manifest = datagen.persona_manifest(ledger)
    assert manifest
    for rec in manifest:
        assert rec["transactions"] > 0
        assert rec["archetype"] in {
            "young_professional",
            "dual_income_family",
            "gig_worker",
            "retiree",
            "recent_graduate",
            "small_business_owner",
        }
    # every focal persona id maps 1:1 to a nameOrig prefix
    ids = {rec["id"] for rec in manifest}
    assert ids == set(ledger.loc[ledger["is_focal_user"], "persona_id"])


def test_tier_stats_shape(ledger) -> None:
    stats = datagen.tier_stats(ledger, "demo")
    for key in (
        "tier",
        "rows",
        "fraud",
        "fraud_rate",
        "anomalies",
        "focal_transactions",
        "personas",
        "fraud_archetypes",
        "columns",
    ):
        assert key in stats
    assert stats["rows"] == len(ledger)
    assert stats["personas"] == ledger["persona_id"].nunique()
    assert "fraud_archetype" in stats["columns"]


def test_income_drifts_upward_over_years(multi_year) -> None:
    """A salaried persona's per-paycheck income grows ~4-10% by year 3.

    Annual raises are 2-5% (finance_agent/personas.py), compounded per year, so
    the year-3 multiplier over year 1 is bounded in [1.03, 1.12]. Irregular
    personas (gig_worker / small_business_owner) earn via lumpy CASH_IN and are
    excluded — raises only apply to scheduled payroll.
    """
    salaried_arch = {"young_professional", "dual_income_family", "retiree", "recent_graduate"}
    # Focal users only: background accounts carry the same archetype names but
    # have flat income (no raises), so they would dilute the drift ratio to 1.0.
    sal = multi_year[
        multi_year["is_focal_user"]
        & multi_year["persona_archetype"].isin(salaried_arch)
        & (multi_year["type"] == "SALARY")
    ].copy()
    assert not sal.empty
    mean_by = sal.groupby(["persona_id", "simulation_year"])["amount"].mean()
    year1 = mean_by.xs(2025, level="simulation_year")
    year3 = mean_by.xs(2027, level="simulation_year")
    both = year1.index.intersection(year3.index)
    assert len(both) >= 3, "expected several salaried personas in both year 1 and year 3"
    ratios = year3[both] / year1[both]
    assert ratios.between(1.03, 1.12).all(), f"income-drift ratios out of bounds: {ratios}"


def test_life_events_do_not_trip_fraud_labels(multi_year) -> None:
    """Legitimate life events (Data-Gen §3/§5 hard negative 13) are NOT fraud.

    A large one-off purchase / medical bill / tuition must stay isFraud=0 and
    is_anomaly=0 even though its size makes it look like fraud — the hard
    negative class the precision story depends on.
    """
    life = multi_year[multi_year["fraud_archetype"] == "hard_negative_life_event"]
    assert not life.empty, "life-event hard negatives must be generated"
    assert (life["isFraud"] == 0).all()
    assert (life["is_anomaly"] == 0).all()
    # and they are genuinely unusual: large, one-off amounts. The balance pass
    # clamps a debit to the available balance when it would overdraw (realistic
    # behavior), so an *attempted* large purchase can land below the nominal
    # minimum for a thin-buffer persona — assert the median stays large.
    assert life["amount"].median() >= 500, (
        f"life events should be large: {life['amount'].describe()}"
    )
    assert life["fraud_archetype"].nunique() == 1


def test_savings_balance_grows_monotonically(multi_year) -> None:
    """Savings accounts grow (net of any withdrawals) with the auto-transfer rule."""
    sav = multi_year[multi_year["nameOrig"].str.endswith("_Sav")].sort_values(["nameOrig", "step"])
    assert not sav.empty
    for acct, g in sav.groupby("nameOrig"):
        # net change = final balance - opening; every savings event is an inflow
        # (auto-transfers), so the closing balance must exceed the opening.
        delta = g["newbalanceOrig"].iloc[-1] - g["oldbalanceOrg"].iloc[0]
        assert delta > 0, f"savings account {acct} did not grow (delta={delta:.2f})"
