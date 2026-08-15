"""Phase A.3 — canary / shadow evaluation tests.

The acceptance claim is concrete and checkable: a deliberately-regressed
candidate model must trigger the ``REGRESSION`` verdict (and therefore the
``canary-regression`` PR label in retrain.yml), while a model that matches the
incumbent must come back ``CLEAN``. The full-pipeline tests run real bundles
(DummyClassifiers) through the exact same scoring path the workflow uses.
"""

import subprocess
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
import yaml

from model_bench import canary

ROOT = Path(__file__).resolve().parent.parent
OLD = "archetype"
RECALL = "recall"
PRECISION = "precision"
SUPPORT = "support"

_TABLE_COLS = [OLD, RECALL, PRECISION, SUPPORT]


@pytest.fixture(scope="module")
def ledger_df() -> pd.DataFrame:
    """Hermetic generated ledger (same params as tests/test_retrieval.py)."""
    from generate_data import generate

    return generate(
        days=90,
        seed=11,
        user="U_Alex",
        n_background_accounts=60,
        start_date="2025-01-01",
    )


def _table(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=_TABLE_COLS)


def _write_bundle(path: Path, constant: int) -> None:
    """Bundle dict shaped exactly like train_and_compare's risk_model_bundle.joblib."""
    from sklearn.dummy import DummyClassifier

    # Must be fitted before dumping — an unfitted estimator raises NotFittedError
    # when scored (predict_proba). A constant classifier makes the mock fully
    # deterministic: constant=1 -> recall 1.0 for every archetype, constant=0 -> 0.0.
    model = DummyClassifier(strategy="constant", constant=constant)
    model.fit(np.zeros((2, 4)), np.array([0, 1]))
    joblib.dump(
        {
            "best_model": model,
            "best_model_name": "dummy",
            "isolation_forest": None,
            "scaler": None,
            "feature_names": [],
            "needs_scaling": False,
            "metrics": {},
        },
        path,
    )


def _write_config(path: Path) -> None:
    cfg = {"model_bench": {"test_size": 0.25, "canary_tolerance": 0.05}}
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)


@pytest.fixture()
def env(ledger_df, tmp_path):
    data_path = tmp_path / "transactions.csv"
    ledger_df.to_csv(data_path, index=False)
    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path)
    old_path = tmp_path / "old_bundle.joblib"
    new_path = tmp_path / "new_bundle.joblib"
    return {
        "data": str(data_path),
        "config": str(cfg_path),
        "old": str(old_path),
        "new": str(new_path),
        "tmp": tmp_path,
    }


def _holdout_fraud(ledger_df: pd.DataFrame) -> int:
    d = ledger_df.sort_values("step", kind="stable").reset_index(drop=True)
    return int(d.iloc[int(len(d) * 0.75) :]["isFraud"].sum())


# ------------------------------------------------------------ diff table unit
def test_diff_table_within_tolerance_is_clean():
    old = _table([{OLD: "easy_structural", RECALL: 1.0, PRECISION: 1.0, SUPPORT: 5}])
    new = _table([{OLD: "easy_structural", RECALL: 0.96, PRECISION: 1.0, SUPPORT: 5}])
    rows, regressed = canary.diff_table(old, new, tolerance=0.05)
    assert regressed == []  # 4 pp drop < 5 pp tolerance
    assert rows[0]["delta_pp"] == pytest.approx(-4.0)
    assert rows[0]["old"] == pytest.approx(1.0)


def test_diff_table_drop_exactly_at_tolerance_is_clean():
    """The spec is 'drops by more than tolerance' — an exact -5 pp drop (with
    a 0.05 tolerance) must stay clean, not flip to a regression."""
    old = _table([{OLD: "easy_structural", RECALL: 1.0, PRECISION: 1.0, SUPPORT: 5}])
    new = _table([{OLD: "easy_structural", RECALL: 0.95, PRECISION: 1.0, SUPPORT: 5}])
    rows, regressed = canary.diff_table(old, new, tolerance=0.05)
    assert regressed == []
    assert rows[0]["delta_pp"] == pytest.approx(-5.0)


def test_diff_table_regression_beyond_tolerance():
    old = _table([{OLD: "easy_structural", RECALL: 1.0, PRECISION: 1.0, SUPPORT: 5}])
    new = _table([{OLD: "easy_structural", RECALL: 0.9, PRECISION: 1.0, SUPPORT: 5}])
    rows, regressed = canary.diff_table(old, new, tolerance=0.05)
    assert regressed == ["easy_structural"]  # 10 pp drop > 5 pp tolerance
    assert rows[0]["delta_pp"] == pytest.approx(-10.0)


def test_diff_table_new_archetype_is_not_a_regression():
    old = _table([{OLD: "balance_drain", RECALL: 1.0, PRECISION: 1.0, SUPPORT: 3}])
    new = _table(
        [
            {OLD: "balance_drain", RECALL: 1.0, PRECISION: 1.0, SUPPORT: 3},
            {OLD: "adversarial_mimicry", RECALL: 0.5, PRECISION: 1.0, SUPPORT: 2},
        ]
    )
    rows, regressed = canary.diff_table(old, new, tolerance=0.05)
    assert regressed == []
    by_arch = {r["archetype"]: r for r in rows}
    assert by_arch["adversarial_mimicry"]["old"] is None  # honest n/a, never flagged
    assert by_arch["adversarial_mimicry"]["delta_pp"] is None


# ------------------------------------------------------------ render body
def test_render_body_regression_mentions_label_and_archetype():
    rows, regressed = canary.diff_table(
        _table([{OLD: "account_takeover", RECALL: 1.0, PRECISION: 1.0, SUPPORT: 2}]),
        _table([{OLD: "account_takeover", RECALL: 0.0, PRECISION: 1.0, SUPPORT: 2}]),
        tolerance=0.05,
    )
    body = canary.render_body(rows, regressed, tolerance=0.05, has_baseline=True)
    assert "account_takeover" in body
    assert "Regression warning" in body
    assert "canary-regression" in body
    assert "**-100.0** ⚠️" in body
    assert "| Incumbent recall @0.5 |" in body


def test_render_body_clean_note():
    body = canary.render_body([], [], tolerance=0.05, has_baseline=True)
    assert "No fraud-archetype rows" in body


def test_render_body_no_baseline_note():
    rows = [{OLD: "x", "old": 1.0, "new": 1.0, "delta_pp": 0.0, "support": 1}]
    body = canary.render_body(rows, [], tolerance=0.05, has_baseline=False)
    assert "No incumbent bundle found in git" in body
    assert "canary-regression" not in body


# ------------------------------------------------------------ full pipeline
def test_run_no_baseline_is_clean(env):
    _write_bundle(env["new"], 1)  # candidate present; incumbent deliberately missing
    verdict, body = canary.run(
        old_path=str(env["tmp"] / "missing.joblib"),
        new_path=env["new"],
        data_path=env["data"],
        config_path=env["config"],
        out_path=str(env["tmp"] / "body.md"),
    )
    assert verdict == "CLEAN"
    assert "No incumbent bundle found in git" in body


@pytest.mark.parametrize("old_const,new_const,expected", [(1, 0, "REGRESSION"), (1, 1, "CLEAN")])
def test_run_verdict_matches_candidate_quality(env, old_const, new_const, expected):
    """Acceptance (A.3): a deliberately-regressed mock candidate triggers the
    warning verdict; a candidate identical to the incumbent stays clean."""
    if _holdout_fraud(pd.read_csv(env["data"])) == 0:
        pytest.skip("fixture holdout has no fraud rows to compare")
    _write_bundle(env["old"], old_const)
    _write_bundle(env["new"], new_const)
    verdict, body = canary.run(
        env["old"], env["new"], env["data"], env["config"], out_path=env["tmp"] / "body.md"
    )
    assert verdict == expected
    if expected == "REGRESSION":
        assert "Regression warning" in body
        assert "canary-regression" in body
    else:
        assert "No archetype regressed" in body


def test_run_missing_candidate_raises(env):
    _write_bundle(env["old"], 1)  # candidate deliberately absent
    with pytest.raises(RuntimeError):
        canary.run(env["old"], env["new"], env["data"], env["config"])


def test_score_bundle_missing_returns_none(env):
    assert canary.score_bundle(str(env["tmp"] / "nope.joblib"), np.zeros((3, 32))) is None


def test_run_feature_schema_mismatch_fails_loudly(env):
    """A bundle trained on a different feature schema must fail the canary
    loudly — never silently score misaligned columns (the canary is the gate
    that decides human sign-off)."""
    _write_bundle(env["old"], 1)
    _write_bundle(env["new"], 1)
    with open(env["new"], "rb") as fh:
        bundle = joblib.load(fh)
    bundle["feature_names"] = ["only_one_feature"]  # wrong schema
    joblib.dump(bundle, env["new"])
    with pytest.raises(RuntimeError, match="feature schema mismatch"):
        canary.run(env["old"], env["new"], env["data"], env["config"])


def test_score_bundle_all_one_vs_all_zero(env):
    X = np.zeros((3, 4))
    _write_bundle(env["old"], 1)
    _write_bundle(env["new"], 0)
    assert np.allclose(canary.score_bundle(env["old"], X), 1.0)
    assert np.allclose(canary.score_bundle(env["new"], X), 0.0)


# ------------------------------------------------------------ CLI (workflow path)
def test_cli_prints_verdict_only_and_writes_body(env):
    """The exact shape retrain.yml relies on: stdout == verdict token only,
    markdown body in --out, exit 0."""
    if _holdout_fraud(pd.read_csv(env["data"])) == 0:
        pytest.skip("fixture holdout has no fraud rows to compare")
    _write_bundle(env["old"], 1)
    _write_bundle(env["new"], 0)
    body_path = env["tmp"] / "body.md"
    proc = subprocess.run(
        [
            sys.executable,
            "model_bench/canary.py",
            "--old",
            env["old"],
            "--new",
            env["new"],
            "--data",
            env["data"],
            "--config",
            env["config"],
            "--out",
            str(body_path),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "REGRESSION"  # nothing else on stdout
    assert "Regression warning" in body_path.read_text(encoding="utf-8")


def test_cli_missing_candidate_exits_2(env):
    _write_bundle(env["old"], 1)
    proc = subprocess.run(
        [
            sys.executable,
            "model_bench/canary.py",
            "--old",
            env["old"],
            "--new",
            str(env["tmp"] / "missing.joblib"),
            "--data",
            env["data"],
            "--config",
            env["config"],
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=300,
    )
    assert proc.returncode == 2
    assert "candidate bundle missing" in proc.stderr
