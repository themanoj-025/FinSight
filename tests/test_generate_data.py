"""Generator tests (Phase 3.6 / 2.1 / 2.2 / 2.3): determinism, seed 0,
ledger balance continuity, and source-list-derived subscription totals."""

import numpy as np
import pandas as pd
import pytest

import generate_data
from generate_data import generate

SMALL = dict(days=20, seed=11, user="U_Alex", n_background_accounts=10, n_fraud_pairs=2)


def test_same_seed_is_deterministic() -> None:
    a = generate(**SMALL)
    b = generate(**SMALL)
    pd.testing.assert_frame_equal(a, b)
    assert len(a) == len(b) > 0


def test_seed_zero_is_respected() -> None:
    """--seed 0 is a valid value and must not be overridden by defaults."""
    a = generate(**{**SMALL, "seed": 0})
    b = generate(**{**SMALL, "seed": 0})
    c = generate(**{**SMALL, "seed": 42})
    pd.testing.assert_frame_equal(a, b)
    assert not a.equals(c)


def test_ledger_balance_continuity() -> None:
    df = generate(days=30, seed=7, n_background_accounts=15, n_fraud_pairs=3)
    focal = df[df["is_focal_user"]].sort_values("step").reset_index(drop=True)
    assert len(focal) > 0
    for _, row in focal.iterrows():
        expected_new = (
            row["oldbalanceOrg"] + row["amount"]
            if row["type"] in {"SALARY", "CASH_IN"}
            else row["oldbalanceOrg"] - row["amount"]
        )
        assert np.isclose(row["newbalanceOrig"], expected_new, atol=0.02), (
            f"row step={row['step']} {row['type']} ${row['amount']}: "
            f"{row['oldbalanceOrg']} -> {row['newbalanceOrig']} (expected {expected_new})"
        )
        assert row["newbalanceOrig"] >= -0.02
    # consecutive focal rows chain old -> new
    for i in range(1, len(focal)):
        assert np.isclose(
            focal.loc[i, "oldbalanceOrg"], focal.loc[i - 1, "newbalanceOrig"], atol=0.02
        )
    # the injected balance-drain anomaly is present and internally consistent
    drains = focal[focal["anomaly_type"] == "balance_drain"]
    assert not drains.empty
    for _, row in drains.iterrows():
        assert row["type"] == "TRANSFER"
        assert row["newbalanceOrig"] <= 0.5 * row["oldbalanceOrg"]


def test_multiple_focal_users_each_get_full_balanced_ledgers() -> None:
    """Multi-user mode: every focal user gets a balanced, anomaly-injected ledger."""
    users = ["U_Alex", "U_Maria", "U_Noah"]
    df = generate(
        days=40,
        seed=7,
        focal_users=users,
        n_background_accounts=10,
        n_fraud_pairs=2,
    )
    # all focal users present and marked focal
    assert set(df.loc[df["is_focal_user"], "nameOrig"].unique()) == set(users)
    for u in users:
        focal = df[df["nameOrig"] == u].sort_values("step").reset_index(drop=True)
        assert len(focal) > 0, f"{u} should have a ledger"
        # balances chain correctly for each user's own ledger
        for _, row in focal.iterrows():
            expected_new = (
                row["oldbalanceOrg"] + row["amount"]
                if row["type"] in {"SALARY", "CASH_IN"}
                else row["oldbalanceOrg"] - row["amount"]
            )
            assert np.isclose(row["newbalanceOrig"], expected_new, atol=0.02), (
                f"{u} step={row['step']}"
            )
        for i in range(1, len(focal)):
            assert np.isclose(
                focal.loc[i, "oldbalanceOrg"], focal.loc[i - 1, "newbalanceOrig"], atol=0.02
            )
    # deterministic across runs
    again = generate(
        days=40,
        seed=7,
        focal_users=users,
        n_background_accounts=10,
        n_fraud_pairs=2,
    )
    pd.testing.assert_frame_equal(df, again)


def test_tiny_window_does_not_crash() -> None:
    """generate() must not crash for windows too small for background fraud pairs.

    `_inject_background_fraud_pairs` used `rng.integers(5, days)`, which raises
    `ValueError: low >= high` for days <= 5. Tiny windows now skip the pair
    injection entirely (there is no room for a full drain cycle anyway).
    """
    df = generate(days=2, seed=3, n_background_accounts=4, n_fraud_pairs=2)
    assert len(df) > 0
    assert "isFraud" in df.columns
    assert (df["isFraud"] == 1).sum() == 0  # no room for fraud pairs in 2 days
    # columns are identical to a normal run (small account pool keeps it fast)
    assert list(df.columns) == list(generate(days=30, seed=3, n_background_accounts=4).columns)


def test_generator_emits_focal_cash_in() -> None:
    """The generator can emit CASH_IN for the focal user (2.10 unmask)."""
    df = generate(days=90, seed=5, n_background_accounts=10, n_fraud_pairs=2)
    focal_cash_in = df[(df["is_focal_user"]) & (df["type"] == "CASH_IN")]
    assert len(focal_cash_in) > 0


def test_subscription_total_tracks_source_list(monkeypatch) -> None:
    base = generate_data.subscription_total()
    assert base == pytest.approx(sum(generate_data.SUBSCRIPTION_AMOUNTS.values()))
    assert base > 100.0  # sanity: several subscriptions
    monkeypatch.setitem(generate_data.SUBSCRIPTION_AMOUNTS, "FakeSub", 10.0)
    assert generate_data.subscription_total() == pytest.approx(base + 10.0)


def test_cli_respects_seed_zero(monkeypatch, tmp_path, capsys) -> None:
    """End-to-end: `--seed 0` must not fall back to the default seed.

    ``main()`` calls ``datagen.generate_dataset`` (not the legacy ``generate``
    wrapper), so the monkeypatch targets the engine and records the resolved
    kwargs. ``--seed 0`` is a falsy int and must be passed through untouched.
    """
    import sys

    from finance_agent import datagen

    out = tmp_path / "out.csv"
    seen: list[dict] = []
    real_generate_dataset = datagen.generate_dataset

    def fake_generate_dataset(**kwargs):
        seen.append(kwargs)
        # Return a tiny frame so the CLI's stats/to_csv path runs quickly.
        return real_generate_dataset(
            days=10, seed=int(kwargs["seed"]), tier="tiny", n_background_accounts=5
        )

    monkeypatch.setattr(datagen, "generate_dataset", fake_generate_dataset)
    monkeypatch.setattr(
        sys, "argv", ["generate_data.py", "--seed", "0", "--tier", "tiny", "--output", str(out)]
    )
    generate_data.main()
    assert seen, "generate_dataset should have been called"
    assert seen[0]["seed"] == 0


@pytest.mark.slow
def test_seed_determinism_at_scale() -> None:
    """Data-Gen §9.4: a demo-tier, mid-size population reproduces byte-identically.

    Each persona draws from its own ``SeedSequence`` substream, so a reordered
    or repeated run must produce the exact same ledger. Content-hash the frame
    instead of a full diff for speed.
    """
    import hashlib

    from finance_agent import datagen

    kw = dict(days=60, seed=7, tier="demo", n_background_accounts=150)

    def digest(df: pd.DataFrame) -> str:
        return hashlib.sha256(
            pd.util.hash_pandas_object(df, index=False).values.tobytes()
        ).hexdigest()

    a = datagen.generate_dataset(**kw)
    b = datagen.generate_dataset(**kw)
    assert len(a) > 1000, "demo-tier ledger should be substantially larger than tiny"
    assert digest(a) == digest(b)
    # a different seed must diverge (guards against a degenerate constant stream)
    c = datagen.generate_dataset(**{**kw, "seed": 8})
    assert digest(a) != digest(c)
