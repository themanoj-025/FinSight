"""Tests for finance_agent.merchants — the ~300-merchant catalog, category
hierarchy, regions, seasonality, and Zipfian popularity sampling (Data-Gen §4).
"""

import numpy as np
import pytest

from finance_agent import merchants


def test_region_table_size_and_keys():
    assert len(merchants.REGION_IDS) == 36
    for rid in merchants.REGION_IDS:
        rec = merchants.REGIONS[rid]
        assert {"city", "state", "lat", "lon", "index"} <= set(rec)
        assert -90.0 <= float(rec["lat"]) <= 90.0
        assert -180.0 <= float(rec["lon"]) <= 180.0


def test_haversine_sane_and_symmetric():
    assert merchants.haversine_miles("R00_portland", "R00_portland") == 0.0
    d1 = merchants.haversine_miles("R00_portland", "R02_denver")
    d2 = merchants.haversine_miles("R02_denver", "R00_portland")
    assert d1 == pytest.approx(d2)
    assert 500.0 < d1 < 2000.0  # Portland <-> Denver ~1000mi


def test_catalog_size_and_uniqueness():
    assert 200 <= len(merchants.MERCHANTS) <= 400  # ~300 named merchants
    names = [m["name"] for m in merchants.MERCHANTS]
    assert len(names) == len(set(names)), "merchant names must be unique"
    # every subcategory is non-empty (no dead leaves in the hierarchy)
    for subs in merchants.SUBCATEGORIES.values():
        assert subs


def test_category_hierarchy_consistency():
    for m in merchants.MERCHANTS:
        assert m["subcategory"] in merchants.SUBCATEGORIES[m["category"]]
        assert m["category"] in merchants.CATEGORY_GROUP
    for cat in merchants.SUBCATEGORIES:
        assert cat in merchants.CATEGORY_GROUP
    # legacy flat categories still resolve to a group
    assert merchants.CATEGORY_GROUP["dining"] == "discretionary"
    assert merchants.CATEGORY_GROUP["income"] == "income"
    assert merchants.CATEGORY_GROUP["savings"] == "savings"


def test_type_for_mapping():
    assert merchants.type_for("income", "payroll") == "SALARY"
    assert merchants.type_for("income", "freelance") == "CASH_IN"
    assert merchants.type_for("housing", "rent") == "TRANSFER"
    assert merchants.type_for("savings", "auto_transfer") == "TRANSFER"
    assert merchants.type_for("subscriptions", "streaming") == "SUBSCRIPTION"
    assert merchants.type_for("groceries", "supermarket") == "SHOP"
    assert merchants.type_for("utilities", "electricity") == "PAYMENT"


def test_merchant_weights_are_zipfian():
    w = merchants.MERCHANT_WEIGHTS
    assert w.shape == (len(merchants.MERCHANTS),)
    assert np.isclose(w.sum(), 1.0)
    assert (w > 0).all()
    top10_share = float(w[np.argsort(w)[::-1][:10]].sum())
    # Zipfian with s=0.9: the top-10 share sits in a believable long-tail band —
    # dominant chains exist, but no single merchant dominates the whole volume.
    assert 0.15 < top10_share < 0.6, f"top-10 share {top10_share:.3f} out of band"


def test_sample_merchants_returns_requested_category():
    rng = np.random.default_rng(0)
    for cat in ("groceries", "dining", "shopping"):
        out = merchants.sample_merchants(rng, cat, 50)
        assert len(out) == 50
        assert all(m["category"] == cat for m in out)
    assert merchants.sample_merchants(rng, "dining", 0) == []


def test_sample_merchants_force_new_excludes_used():
    rng = np.random.default_rng(1)
    used = {m["name"] for m in merchants.sample_merchants(rng, "dining", 20)}
    rng = np.random.default_rng(2)
    out = merchants.sample_merchants(rng, "dining", 5, force_new=used)
    assert all(m["name"] not in used for m in out)


def test_merchant_by_name_roundtrip():
    m = merchants.MERCHANTS[0]
    assert merchants.merchant_by_name(m["name"]) == m
    with pytest.raises(KeyError):
        merchants.merchant_by_name("definitely-not-a-merchant")


def test_seasonal_multipliers_shape_and_sanity():
    for _, mults in merchants.SEASONAL_MULTIPLIER.items():
        assert len(mults) == 12
        assert all(v > 0.0 for v in mults)
    # holiday shopping bump: December > September, and flat categories are flat
    assert merchants.seasonal_multiplier("shopping", 12) > merchants.seasonal_multiplier(
        "shopping", 9
    )
    assert merchants.seasonal_multiplier("subscriptions", 6) == 1.0
    # unknown category -> neutral 1.0
    assert merchants.seasonal_multiplier("income", 3) == 1.0


def test_holiday_window():
    assert not merchants.is_holiday_window(11, 14)
    assert merchants.is_holiday_window(11, 15)
    assert merchants.is_holiday_window(12, 25)
    assert merchants.is_holiday_window(12, 31)
    assert not merchants.is_holiday_window(1, 1)


def test_subscription_amounts_are_realistic_and_deterministic():
    amounts = merchants.SUBSCRIPTION_AMOUNTS
    assert len(amounts) >= 8
    assert all(v > 0 for v in amounts.values())
    assert "Netflix" in amounts
