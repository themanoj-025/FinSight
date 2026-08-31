"""Tests for finance_agent.fraud_patterns — the 15 difficulty-graded pattern
library: labels, determinism, difficulty-tier structure, injection windows,
and the discovery-lag label-realism pass (Data-Gen §5).
"""

import numpy as np

from finance_agent import fraud_patterns as fp
from finance_agent.personas import sample_persona


def _ctx(days: int = 90, seed: int = 7, scale: float = 1.0) -> fp.PatternCtx:
    persona = sample_persona("U_Test", "young_professional", np.random.default_rng(seed))
    return fp.PatternCtx(
        rng=np.random.default_rng(seed),
        persona=persona,
        days=days,
        start=persona_start(),
        bg_pool=["C_BG000001", "C_BG000002", "C_BG000003"],
        scale=scale,
    )


def persona_start() -> None:
    from datetime import datetime

    return datetime(2025, 1, 1)


def test_pattern_catalog_has_15_archetypes() -> None:
    names = fp.pattern_names()
    assert len(names) == 15
    assert "bust_out" in names
    assert set(fp.EASY_PATTERNS + fp.MEDIUM_PATTERNS + fp.HARD_PATTERNS + fp.HARD_NEGATIVES) == set(
        names
    ) - {"bust_out"}
    # every pattern has an injection rate and a minimum window
    for name in names:
        assert name in fp.PATTERN_RATES
        assert name in fp._MIN_DAYS
        assert fp.PATTERN_RATES[name] > 0.0
        assert fp._MIN_DAYS[name] >= 1


def test_easy_patterns_are_labeled_correctly() -> None:
    ctx = _ctx(days=90)
    rows = fp.gen_balance_drain(ctx)
    assert rows
    for r in rows:
        assert r["isFraud"] == 1
        assert r["is_anomaly"] == 1
        assert r["anomaly_type"] == "balance_drain"
        assert r["fraud_archetype"] == "balance_drain"
        assert r["label_reported_at_step"] is None or isinstance(r["label_reported_at_step"], int)

    dup = fp.gen_duplicate_charge(_ctx(days=90))
    assert len(dup) == 2
    # the second charge is the anomaly; the first is a normal row
    assert dup[0]["isFraud"] == 0 and dup[0]["is_anomaly"] == 0
    assert dup[1]["is_anomaly"] == 1 and dup[1]["anomaly_type"] == "duplicate_charge"
    assert dup[0]["amount"] == dup[1]["amount"]  # same merchant + amount

    spike = fp.gen_spend_spike(_ctx(days=90))
    assert len(spike) == 4
    assert all(r["is_anomaly"] == 1 and r["isFraud"] == 0 for r in spike)


def test_hard_negatives_are_legitimate_unusual_rows() -> None:
    ctx = _ctx(days=90)
    for gen in (fp.gen_life_event, fp.gen_travel, fp.gen_rapid_burst):
        rows = gen(ctx)
        assert rows
        for r in rows:
            assert r["isFraud"] == 0
            assert r["is_anomaly"] == 0
            assert r["fraud_archetype"].startswith("hard_negative_")


def test_patterns_are_deterministic_given_same_rng() -> None:
    def gen_once(seed: int) -> list[dict]:
        ctx = _ctx(days=90, seed=seed)
        return fp.gen_new_payee_transfer(ctx)

    assert gen_once(11) == gen_once(11)
    assert gen_once(11) != gen_once(12)


def test_account_takeover_labels_only_the_final_drain() -> None:
    ctx = _ctx(days=120)
    rows = fp.gen_account_takeover(ctx)
    assert rows
    # migration rows are unlabeled; only the final drain + cash-out are fraud
    fraud_rows = [r for r in rows if r["isFraud"] == 1]
    assert fraud_rows
    assert all(r["fraud_archetype"] == "account_takeover" for r in fraud_rows)
    normal = [r for r in rows if r["isFraud"] == 0]
    assert normal, "takeover must include legitimate-looking migration rows"
    assert all(r["is_anomaly"] == 0 for r in normal)


def test_refund_abuse_loop_structure() -> None:
    ctx = _ctx(days=120)
    rows = fp.gen_refund_abuse(ctx)
    assert rows
    refunds = [r for r in rows if r["category"] == "refund"]
    purchases = [r for r in rows if r["category"] == "shopping"]
    assert refunds and purchases
    assert all(r["isFraud"] == 1 for r in rows)


def test_inject_focal_patterns_tiny_window_returns_nothing() -> None:
    """A 2-day window has no room for any pattern to play out."""
    ctx = _ctx(days=2)
    assert fp.inject_focal_patterns(ctx) == []


def test_inject_focal_patterns_full_window_includes_easy_tier() -> None:
    ctx = _ctx(days=90)
    rows = fp.inject_focal_patterns(ctx)
    archetypes = {r["fraud_archetype"] for r in rows}
    # the deterministic easy tier always fires when the window allows
    assert "balance_drain" in archetypes
    assert "duplicate_charge" in archetypes
    assert "spend_spike" in archetypes


def test_inject_focal_patterns_scale_raises_frequency() -> None:
    low = fp.inject_focal_patterns(_ctx(days=120, seed=5, scale=0.0))
    high = fp.inject_focal_patterns(_ctx(days=120, seed=5, scale=1.0))
    assert len(low) < len(high)  # scale 0 keeps only the forced easy tier


def test_inject_background_patterns_bust_out() -> None:
    ctx = _ctx(days=180)
    rows = fp.inject_background_patterns(ctx, bust=True)
    assert rows
    assert all(r["isFraud"] == 1 and r["fraud_archetype"] == "bust_out" for r in rows)
    assert fp.inject_background_patterns(_ctx(days=10), bust=True) == []


def test_discovery_lag_marks_fraud_as_reported_later() -> None:
    ctx = _ctx(days=90)
    rows = fp.gen_balance_drain(ctx)
    rng = np.random.default_rng(3)
    out = fp.apply_discovery_lag(rows, rng, lag_rate=1.0)
    assert any(int(r["label_reported_at_step"]) > int(r["step"]) for r in out)
    # with lag_rate 0 every row is reported at its own step
    out0 = fp.apply_discovery_lag(
        fp.gen_balance_drain(_ctx(days=90)), np.random.default_rng(3), 0.0
    )
    assert all(int(r["label_reported_at_step"]) == int(r["step"]) for r in out0)


def test_pattern_rows_carry_full_v2_metadata() -> None:
    ctx = _ctx(days=90)
    for name in ("card_testing", "subscription_creep", "mimicry", "rapid_burst"):
        rows = fp.gen_pattern(name, ctx)
        assert rows, name
        for r in rows:
            assert r["subcategory"], name
            assert r["transaction_region"], name
            assert r["account_type"] in ("checking", "savings", "credit")
            assert r["merchant"], name
    # seasonal mimicry only plays out inside the Nov 15 - Dec 31 window; a
    # Jan-start window has no room, so it must be an explicit no-op there.
    assert fp.gen_pattern("seasonal_mimicry", _ctx(days=90)) == []
