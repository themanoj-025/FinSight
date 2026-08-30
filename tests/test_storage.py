"""Tests for the SQLite persistence layer (Phase 6 stretch goal).

Covers: hand-rolled migrations, CSV sync (idempotent per fingerprint),
materialized risk_scores, SQL-side threshold/focal/limit semantics, exact
output equivalence with the in-memory path, persistence across instances, and
fingerprint-driven recomputation after the data changes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from finance_agent.storage import SCHEMA_VERSION, TransactionStore
from generate_data import generate


def _env(tmp_path) -> dict[str, object]:
    """Hermetic env with two configs: one with a store, one without (same CSV)."""
    import yaml

    df = generate(
        days=30,
        seed=7,
        user="U_Alex",
        n_background_accounts=20,
        n_fraud_pairs=2,
        start_date="2025-01-01",
    )
    csv = tmp_path / "transactions.csv"
    df.to_csv(csv, index=False)
    db = tmp_path / "transactions.db"

    def write(cfg: dict, name: str) -> str:
        path = tmp_path / name
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False)
        return str(path)

    base = {
        "model_bench": {"bundle_path": str(tmp_path / "missing.joblib")},
        "risk": {
            "blend": {"rules": 0.4, "supervised": 0.3, "isolation_forest": 0.3},
            "fraud_threshold": 0.7,
        },
        "agent": {"activity_log": str(tmp_path / "activity.jsonl"), "model": "claude-sonnet-4-5"},
    }
    cfg_store = {**base, "data": {"path": str(csv), "store_path": str(db)}}
    cfg_nostore = {**base, "data": {"path": str(csv)}}
    return {
        "csv": str(csv),
        "db": str(db),
        "df": df,
        "cfg_store": write(cfg_store, "config_store.yaml"),
        "cfg_nostore": write(cfg_nostore, "config_nostore.yaml"),
    }


def _attach_bundle(env: dict, tmp_path) -> None:
    """Point both configs at a real trained bundle (LGBM + isolation forest)."""
    import joblib
    import yaml
    from lightgbm import LGBMClassifier
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler

    from finance_agent.features import build_features

    df = pd.read_csv(env["csv"])
    X = build_features(df)
    y = df["isFraud"].astype(int).to_numpy()
    bundle = {
        "best_model": LGBMClassifier(n_estimators=20, verbose=-1, random_state=0).fit(X, y),
        "best_model_name": "Gradient Boosting (LightGBM)",
        "isolation_forest": IsolationForest(random_state=0).fit(X),
        "scaler": StandardScaler().fit(X),
        "feature_names": list(X.columns),
        "needs_scaling": False,
        "metrics": {"pr_auc": 0.99},
    }
    bundle_path = tmp_path / "bundle.joblib"
    joblib.dump(bundle, bundle_path)
    for name in ("cfg_store", "cfg_nostore"):
        with open(env[name], encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        cfg["model_bench"]["bundle_path"] = str(bundle_path)
        with open(env[name], "w", encoding="utf-8") as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False)


# ---------------------------------------------------------------- migrations
def test_migrations_apply_from_scratch(tmp_path) -> None:
    store = TransactionStore(str(tmp_path / "t.db"))
    assert store.schema_version() == SCHEMA_VERSION
    assert store.total_rows() == 0
    # tables + indexes exist
    import sqlite3

    conn = sqlite3.connect(store.path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"meta", "transactions", "risk_scores"} <= tables
        idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_risk_score" in idx and "idx_tx_focal" in idx
    finally:
        conn.close()


# -------------------------------------------------------------------- sync
def test_sync_from_frame_loads_all_rows(tmp_path) -> None:
    env = _env(tmp_path)
    store = TransactionStore(str(env["db"]))
    loaded = store.sync_from_frame(env["df"], "fp1")
    assert loaded is True
    assert store.total_rows() == len(env["df"])
    df = store.transactions_df()
    assert list(df.columns) == list(env["df"].columns)
    assert int(df["step"].sum()) == int(env["df"]["step"].sum())


def test_sync_is_idempotent_with_same_fingerprint(tmp_path) -> None:
    env = _env(tmp_path)
    store = TransactionStore(str(env["db"]))
    # first sync reads the CSV file itself (exercises sync_from_csv)
    assert store.sync_from_csv(env["csv"], "fp1") is True
    n = store.total_rows()
    # same fingerprint -> no reload, no duplicate rows
    assert store.sync_from_csv(env["csv"], "fp1") is False
    assert store.total_rows() == n
    assert store.is_synced("fp1") is True
    assert store.is_synced("fp2") is False


# ------------------------------------------------------- materialized scores
def _scored_like(df: pd.DataFrame) -> pd.DataFrame:
    """Deterministic fake scored frame (no model needed) aligned to `df` ids."""
    scored = df.copy()
    scored["_row_index"] = np.arange(len(df))
    scored["risk_score"] = np.round(0.1 * (df["step"] % 11), 3)
    scored["rule_score"] = np.round(scored["risk_score"] / 2, 3)
    scored["model_score"] = 0.0
    scored["isolation_score"] = 0.0
    scored["rule_reason"] = ""
    scored["reason"] = scored["risk_score"].map(lambda s: "spike" if s >= 0.9 else "")
    return scored


def test_materialize_and_query_semantics(tmp_path) -> None:
    env = _env(tmp_path)
    store = TransactionStore(str(env["db"]))
    store.sync_from_frame(env["df"], "csv_fp")
    scored = _scored_like(env["df"])
    store.materialize_risk_scores("risk_fp", scored)
    assert store.is_risk_materialized("risk_fp") is True

    thr = 0.8
    expected_all = env["df"][scored["risk_score"] >= thr]
    expected_focal = expected_all[expected_all["is_focal_user"]]
    assert store.flagged_count(thr, focal_only=False) == len(expected_all)
    assert store.flagged_count(thr, focal_only=True) == len(expected_focal)

    rows = store.risk_scores(threshold=thr, focal_only=True, limit=5)
    assert len(rows) == min(5, len(expected_focal))
    assert list(rows["risk_score"]) == sorted(rows["risk_score"], reverse=True)
    assert all(rows["risk_score"] >= thr)
    assert all(rows["is_focal_user"])
    # SQL tie-break is id ASC (= CSV order), mirroring the pandas stable sort
    top = expected_focal.assign(_s=scored.loc[expected_focal.index, "risk_score"].to_numpy())
    expected_ids = list(top.sort_values("_s", ascending=False, kind="stable").index[:5])
    assert list(rows["_row_index"]) == expected_ids


def test_materialized_scores_persist_across_instances(tmp_path) -> None:
    env = _env(tmp_path)
    store = TransactionStore(str(env["db"]))
    store.sync_from_frame(env["df"], "csv_fp")
    store.materialize_risk_scores("risk_fp", _scored_like(env["df"]))
    # a brand-new store on the same file (fresh process) must see the scores
    again = TransactionStore(str(env["db"]))
    assert again.is_risk_materialized("risk_fp") is True
    assert again.risk_scores(threshold=0.0, focal_only=False, limit=10).shape[0] == 10


# ------------------------------------------------ equivalence with pandas path
@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 10, "threshold": 0.7},
        {"limit": 50, "threshold": 0.2, "focal_only": True},
        {"limit": 20, "threshold": 0.0, "focal_only": False},
        {"limit": 5, "threshold": 1.5, "focal_only": True},
    ],
)
def test_store_path_matches_pandas_path_rule_only(tmp_path, kwargs) -> None:
    from finance_agent.tools import FinanceFacts

    env = _env(tmp_path)
    with_store = FinanceFacts(env["cfg_store"])
    without = FinanceFacts(env["cfg_nostore"])
    assert with_store.store is not None
    assert without.store is None

    a = with_store.risk_scored_transactions(**kwargs)
    b = without.risk_scored_transactions(**kwargs)
    assert a == b, f"store path diverged from pandas path for {kwargs}"


def test_store_path_matches_pandas_path_with_bundle_and_shap(tmp_path) -> None:
    from finance_agent.tools import FinanceFacts

    env = _env(tmp_path)
    _attach_bundle(env, tmp_path)
    with_store = FinanceFacts(env["cfg_store"])
    without = FinanceFacts(env["cfg_nostore"])
    assert not with_store.rule_only()

    a = with_store.risk_scored_transactions(
        limit=8, threshold=0.0, focal_only=True, include_explanations=True
    )
    b = without.risk_scored_transactions(
        limit=8, threshold=0.0, focal_only=True, include_explanations=True
    )
    assert a == b
    assert a["data"]["explanations_available"] is True
    assert all(r.get("explanation") for r in a["data"]["rows"])


def test_store_recomputes_when_data_fingerprint_changes(tmp_path) -> dict[str, object]:
    from finance_agent.tools import FinanceFacts

    env = _env(tmp_path)
    facts = FinanceFacts(env["cfg_store"])
    base = facts.risk_scored_transactions(limit=50, threshold=0.0, focal_only=True)
    base_total = base["data"]["total_scored"]

    # append a high-risk drain pair (transfer + matching cash-out) -> the size
    # and mtime change, forcing a new fingerprint and a re-materialization
    df = pd.read_csv(env["csv"])
    new_step = int(df["step"].max()) + 5000

    def row(step: int, txn_type: str, orig: str, dest: str) -> dict:
        return {
            "step": step,
            "type": txn_type,
            "amount": 2000.0,
            "nameOrig": orig,
            "oldbalanceOrg": 2000.0,
            "newbalanceOrig": 0.0,
            "nameDest": dest,
            "oldbalanceDest": 0.0,
            "newbalanceDest": 0.0,
            "merchant": "WireOut" if txn_type == "TRANSFER" else "ATM Withdrawal",
            "category": "transfer",
            "datetime": "2025-04-01T10:00",
            "date": "2025-04-01",
            "is_focal_user": txn_type == "TRANSFER",
            "isFraud": 1,
            "isFlaggedFraud": 0,
            "is_anomaly": 1,
            "anomaly_type": "balance_drain",
        }

    extra = [
        row(new_step, "TRANSFER", "U_Alex", "C_BG000001"),
        row(new_step + 1, "CASH_OUT", "C_BG000001", "C_ATM"),
    ]
    pd.concat([df, pd.DataFrame(extra)], ignore_index=True).to_csv(env["csv"], index=False)

    fresh = FinanceFacts(env["cfg_store"])
    result = fresh.risk_scored_transactions(limit=50, threshold=0.0, focal_only=True)
    assert result["data"]["total_scored"] == base_total + 2
    # the appended drain transfer now scores 1.0 (rule-only); ties break by id
    # ASC so the oldest 1.0 row sorts first — the new one must still be present
    new_rows = [r for r in result["data"]["rows"] if r["step"] == new_step]
    assert new_rows and new_rows[0]["risk_score"] == 1.0


def test_store_reset_clears_everything(tmp_path) -> None:
    env = _env(tmp_path)
    store = TransactionStore(str(env["db"]))
    store.sync_from_frame(env["df"], "fp")
    store.materialize_risk_scores("risk_fp", _scored_like(env["df"]))
    store.reset()
    assert store.total_rows() == 0
    assert store.is_synced("fp") is False
    assert store.is_risk_materialized("risk_fp") is False
