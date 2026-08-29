"""Tests for finance_agent.tools and finance_agent.agent (offline mode)."""

import json

import pytest

from generate_data import generate


@pytest.fixture()
def tmp_env(tmp_path):
    """A hermetic environment: fresh synthetic data + config in a temp dir."""
    import yaml

    df = generate(
        days=30,
        seed=7,
        user="U_Alex",
        n_background_accounts=20,
        n_fraud_pairs=2,
        start_date="2025-01-01",
    )
    data_path = tmp_path / "transactions.csv"
    df.to_csv(data_path, index=False)
    cfg = {
        "data": {"path": str(data_path)},
        "model_bench": {"bundle_path": str(tmp_path / "missing.joblib")},
        "risk": {
            "blend": {"rules": 0.4, "supervised": 0.3, "isolation_forest": 0.3},
            "fraud_threshold": 0.7,
        },
        "budgets": {"monthly": {"groceries": 1000.0, "dining": 500.0, "health": 200.0}},
        "agent": {
            "activity_log": str(tmp_path / "activity.jsonl"),
            "max_turns": 3,
            "model": "claude-sonnet-4-5",
        },
    }
    cfg_path = tmp_path / "config.yaml"
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)
    return {"cfg_path": str(cfg_path), "tmp": tmp_path, "df": df}


def test_monthly_summary_shape(tmp_env):
    from finance_agent.tools import FinanceFacts

    facts = FinanceFacts(tmp_env["cfg_path"])
    result = facts.monthly_summary()
    data = result["data"]
    assert data["income"] > 0
    assert data["expenses"] > 0
    for key in ("month", "income", "expenses", "net", "savings_rate", "top_category"):
        assert key in data
    assert isinstance(result["summary"], str) and result["summary"]


def test_category_breakdown_sorted_desc(tmp_env):
    from finance_agent.tools import FinanceFacts

    facts = FinanceFacts(tmp_env["cfg_path"])
    rows = facts.category_breakdown()["data"]["rows"]
    assert rows
    amounts = [r["amount"] for r in rows]
    assert amounts == sorted(amounts, reverse=True)
    assert abs(sum(amounts) - facts.category_breakdown()["data"]["total"]) < 1e-6


def test_risk_scored_transactions_shape_and_bounds(tmp_env):
    from finance_agent.tools import FinanceFacts

    facts = FinanceFacts(tmp_env["cfg_path"])  # no bundle -> rule-only scoring
    result = facts.risk_scored_transactions(limit=20, threshold=0.0)
    rows = result["data"]["rows"]
    assert rows
    expected = {
        "step",
        "amount",
        "merchant",
        "category",
        "rule_score",
        "model_score",
        "isolation_score",
        "risk_score",
        "reason",
    }
    assert expected.issubset(rows[0].keys())
    for row in rows:
        assert 0.0 <= row["risk_score"] <= 1.0
    assert result["data"]["total_scored"] == len(tmp_env["df"])


def test_risk_scan_clean_message_at_impossible_threshold(tmp_env):
    from finance_agent.tools import FinanceFacts

    facts = FinanceFacts(tmp_env["cfg_path"])
    result = facts.risk_scored_transactions(limit=10, threshold=1.5)
    assert result["data"]["flagged_count"] == 0
    assert "clean" in result["summary"]


def test_budget_status_reports_goals_and_over_flag(tmp_env):
    """budget_status returns per-category spend vs. goal with an over flag."""
    from finance_agent.tools import FinanceFacts

    facts = FinanceFacts(tmp_env["cfg_path"])
    result = facts.budget_status()
    data = result["data"]
    assert data["configured"] is True
    rows = {r["category"]: r for r in data["rows"]}
    # goals come from the tmp_env config
    assert set(rows) == {"groceries", "dining", "health"}
    for r in rows.values():
        assert r["spent"] >= 0
        assert r["goal"] > 0
        assert 0.0 <= r["pct"] <= 1.0 or r["over"] is True  # over-goal pct can exceed 1
        assert r["over"] == (r["pct"] > 1.0)
    assert isinstance(result["summary"], str) and "budget" in result["summary"]


def test_budget_status_tracks_an_over_goal_category(tmp_env):
    """Force a category over its tiny goal and confirm the over flag + summary."""
    import yaml

    from finance_agent.tools import FinanceFacts

    cfg_path = tmp_env["cfg_path"]
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg["budgets"] = {"monthly": {"dining": 1.0, "groceries": 100000.0}}  # dining always over
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)

    facts = FinanceFacts(cfg_path)
    data = facts.budget_status()["data"]
    by_cat = {r["category"]: r for r in data["rows"]}
    assert by_cat["dining"]["over"] is True
    assert by_cat["dining"]["pct"] > 1.0
    assert by_cat["groceries"]["over"] is False
    assert "over" in facts.budget_status()["summary"].lower()


def test_budget_status_unconfigured_is_graceful(tmp_env):
    """No budgets.monthly -> configured: False and a helpful summary, no crash."""
    import yaml

    from finance_agent.tools import FinanceFacts

    cfg_path = tmp_env["cfg_path"]
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.pop("budgets", None)
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)

    facts = FinanceFacts(cfg_path)
    data = facts.budget_status()["data"]
    assert data["configured"] is False
    assert data["rows"] == []


def test_focal_user_selection_switches_monthly_summary(tmp_path):
    """Multi-user: FinanceFacts(focal_user=...) reports on the selected user."""
    import yaml

    from finance_agent.tools import FinanceFacts

    users = ["U_Alex", "U_Maria"]
    df = generate(
        days=40,
        seed=7,
        focal_users=users,
        n_background_accounts=15,
        n_fraud_pairs=2,
    )
    data_path = tmp_path / "transactions.csv"
    df.to_csv(data_path, index=False)
    cfg = {
        "data": {"path": str(data_path), "focal_users": users, "focal_user": "U_Alex"},
        "model_bench": {"bundle_path": str(tmp_path / "missing.joblib")},
        "risk": {
            "blend": {"rules": 0.4, "supervised": 0.3, "isolation_forest": 0.3},
            "fraud_threshold": 0.7,
        },
        "agent": {"activity_log": str(tmp_path / "a.jsonl"), "model": "claude-sonnet-4-5"},
    }
    cfg_path = tmp_path / "config.yaml"
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)

    alex = FinanceFacts(cfg_path)  # default focal_user from config = U_Alex
    assert alex.focal_user == "U_Alex"
    assert set(alex.focal_users) == set(users)
    assert set(alex._focal()["nameOrig"].unique()) == {"U_Alex"}

    maria = FinanceFacts(cfg_path, focal_user="U_Maria")
    assert maria.focal_user == "U_Maria"
    assert set(maria._focal()["nameOrig"].unique()) == {"U_Maria"}

    # different per-user monthly numbers (incomes differ per user)
    a = alex.monthly_summary()["data"]
    b = maria.monthly_summary()["data"]
    assert a["income"] > 0 and b["income"] > 0
    assert a != b
    assert alex.financial_health() != maria.financial_health()
    # risk scan with focal_only=True is scoped to the selected user
    alex_rows = alex.risk_scored_transactions(limit=50, threshold=0.0, focal_only=True)
    assert all(r["nameOrig"] == "U_Alex" for r in alex_rows["data"]["rows"])
    maria_rows = maria.risk_scored_transactions(limit=50, threshold=0.0, focal_only=True)
    assert all(r["nameOrig"] == "U_Maria" for r in maria_rows["data"]["rows"])


def test_forecast_keys(tmp_env):
    from finance_agent.tools import FinanceFacts

    facts = FinanceFacts(tmp_env["cfg_path"])
    data = facts.forecast_next_month()["data"]
    for key in ("forecast_income", "forecast_expenses", "forecast_net", "trend", "history"):
        assert key in data


def test_agent_offline_fallback_and_activity_log(tmp_env):
    from finance_agent.agent import FinanceAgent

    agent = FinanceAgent(tmp_env["cfg_path"], api_key="")
    assert not agent.llm_available()
    reply = agent.answer("Any suspicious activity?")
    assert isinstance(reply, str) and reply.strip()
    assert "risk" in reply.lower()
    entries = agent.activity_log()
    assert entries, "offline narrator should log every tool call"
    assert {"tool", "latency_ms", "ok"}.issubset(entries[0].keys())
    # the JSONL file itself exists and is valid
    log_path = tmp_env["tmp"] / "activity.jsonl"
    assert log_path.exists()
    with open(log_path, encoding="utf-8") as fh:
        assert json.loads(fh.readlines()[0])["tool"]


def test_agent_streams_text_chunks(tmp_env):
    from finance_agent.agent import FinanceAgent

    agent = FinanceAgent(tmp_env["cfg_path"], api_key="")
    chunks = list(agent.answer("How can I save more?", stream=True))
    assert chunks
    assert "".join(chunks) == agent.answer("How can I save more?")


def test_report_builds_markdown(tmp_env):
    from finance_agent.report import build_report
    from finance_agent.tools import FinanceFacts

    md = build_report(FinanceFacts(tmp_env["cfg_path"]))
    assert md.startswith("# FinSight Agent")
    assert "## Fraud & anomaly scan" in md
    assert "## Model card" in md


def test_rule_only_mode_can_still_flag_high_confidence_fraud(tmp_env):
    """0.2: with no model bundle the blend renormalizes to rule_score (weight 1.0),
    so a transaction tripping detect_balance_drain (rule_score=1.0) still clears
    the configured threshold of 0.7."""
    import pandas as pd

    from finance_agent.tools import FinanceFacts

    df = tmp_env["df"].copy()
    rows = [
        {
            "step": 100_000,
            "type": "TRANSFER",
            "amount": 2000.0,
            "nameOrig": "U_Alex",
            "oldbalanceOrg": 2000.0,
            "newbalanceOrig": 0.0,
            "nameDest": "C_BG000001",
            "oldbalanceDest": 0.0,
            "newbalanceDest": 0.0,
            "merchant": "WireOut",
            "category": "transfer",
            "datetime": "2025-03-01T10:00",
            "date": "2025-03-01",
            "is_focal_user": True,
            "isFraud": 1,
            "isFlaggedFraud": 0,
            "is_anomaly": 1,
            "anomaly_type": "balance_drain",
        },
        {
            "step": 100_001,
            "type": "CASH_OUT",
            "amount": 2000.0,
            "nameOrig": "C_BG000001",
            "oldbalanceOrg": 2000.0,
            "newbalanceOrig": 0.0,
            "nameDest": "C_ATM",
            "oldbalanceDest": 0.0,
            "newbalanceDest": 0.0,
            "merchant": "ATM Withdrawal",
            "category": "transfer",
            "datetime": "2025-03-01T11:00",
            "date": "2025-03-01",
            "is_focal_user": False,
            "isFraud": 1,
            "isFlaggedFraud": 0,
            "is_anomaly": 1,
            "anomaly_type": "balance_drain",
        },
    ]
    df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
    df.to_csv(tmp_env["tmp"] / "transactions.csv", index=False)  # refresh on-disk data

    facts = FinanceFacts(tmp_env["cfg_path"])
    assert facts.rule_only()
    result = facts.risk_scored_transactions(limit=50, threshold=0.7, focal_only=True)
    flagged = [r for r in result["data"]["rows"] if r["step"] == 100_000]
    assert flagged, "balance-drain row must clear the 0.7 threshold in rule-only mode"
    assert flagged[0]["risk_score"] == 1.0


def test_monthly_summary_excludes_cash_in_from_expenses(tmp_env):
    """2.10: a CASH_IN credit must never appear in the expense total."""
    import pandas as pd

    from finance_agent import rules
    from finance_agent.tools import FinanceFacts

    df = tmp_env["df"].copy()
    max_step = int(df["step"].max())
    row = {
        "step": max_step + 1,
        "type": "CASH_IN",
        "amount": 5000.0,
        "nameOrig": "U_Alex",
        "oldbalanceOrg": 0.0,
        "newbalanceOrig": 5000.0,
        "nameDest": "C_Freelance",
        "oldbalanceDest": 0.0,
        "newbalanceDest": 0.0,
        "merchant": "Freelance Income",
        "category": "income",
        "datetime": "2025-04-15T12:00",
        "date": "2025-04-15",
        "is_focal_user": True,
        "isFraud": 0,
        "isFlaggedFraud": 0,
        "is_anomaly": 0,
        "anomaly_type": "",
        # data-gen v2 columns: `_focal` filters by persona_id/account_type, so
        # an injected row must carry them or it silently drops out of the view
        "persona_id": "U_Alex",
        "persona_archetype": "young_professional",
        "account_type": "checking",
        "merchant_region": "R00_portland",
        "transaction_region": "R00_portland",
        "home_region": "R00_portland",
        "category_group": "income",
        "subcategory": "freelance",
        "fraud_archetype": "",
        "label_reported_at_step": max_step + 1,
        "simulation_year": 2025,
    }
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(tmp_env["tmp"] / "transactions.csv", index=False)

    facts = FinanceFacts(tmp_env["cfg_path"])
    month = facts.monthly_summary()["data"]
    focal = facts._focal()
    focal["_month"] = pd.to_datetime(focal["datetime"]).dt.strftime("%Y-%m")
    this_month = focal[focal["_month"] == month["month"]]
    expected_expenses = float(this_month.loc[rules.expense_rows(this_month), "amount"].sum())
    assert month["expenses"] == pytest.approx(expected_expenses)
    # the credit counts as income, not spending
    assert month["income"] >= 5000.0
    assert month["expenses"] < month["income"]


def _bundle_env(tmp_env):
    """Return a tmp_env variant whose config points at a real trained bundle."""
    import joblib
    import yaml
    from lightgbm import LGBMClassifier
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler

    from finance_agent.features import build_features

    df = tmp_env["df"]
    X = build_features(df)
    y = df["isFraud"].astype(int).to_numpy()
    clf = LGBMClassifier(n_estimators=20, verbose=-1, random_state=0).fit(X, y)
    bundle = {
        "best_model": clf,
        "best_model_name": "Gradient Boosting (LightGBM)",
        "isolation_forest": IsolationForest(random_state=0).fit(X),
        "scaler": StandardScaler().fit(X),
        "feature_names": list(X.columns),
        "needs_scaling": False,
        "metrics": {"pr_auc": 0.99},
    }
    bundle_path = tmp_env["tmp"] / "bundle.joblib"
    joblib.dump(bundle, bundle_path)
    cfg_path = tmp_env["cfg_path"]
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg["model_bench"]["bundle_path"] = str(bundle_path)
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)
    return tmp_env


def test_shap_explanations_attached_when_bundle_present(tmp_env):
    """Phase 6: with a model bundle, include_explanations returns per-transaction
    TreeSHAP contributions that sum (with the bias) to the model's log-odds."""
    import math

    from finance_agent.tools import FinanceFacts

    env = _bundle_env(tmp_env)
    facts = FinanceFacts(env["cfg_path"])
    assert not facts.rule_only()
    result = facts.risk_scored_transactions(
        limit=10, threshold=0.0, include_explanations=True, focal_only=True
    )
    assert result["data"]["explanations_available"] is True
    rows = result["data"]["rows"]
    assert rows
    for row in rows:
        expl = row.get("explanation")
        assert expl is not None, "every displayed row should carry an explanation"
        assert "top_features" in expl and "bias" in expl and "all_features" in expl
        assert expl["top_features"], "top_features must not be empty"
        # SHAP identity: sum(all contributions) + bias == logit(model_score).
        # model_score is rounded to 3 decimals in the scored frame, and the
        # logit derivative near the small fraud probabilities here amplifies
        # that rounding (~0.08), so use a tolerance that reflects it.
        logit = sum(f["contribution"] for f in expl["all_features"]) + expl["bias"]
        proba = row["model_score"]
        expected_logit = math.log(proba / (1 - proba)) if 0 < proba < 1 else logit
        assert abs(logit - expected_logit) < 0.15, f"SHAP sum mismatch: {logit} vs {expected_logit}"
        # top_features is a strict subset of all_features, sorted by |contribution|
        abs_vals = [f["contribution"] for f in expl["top_features"]]
        assert abs_vals == sorted(abs_vals, key=abs, reverse=True)


def test_shap_explanations_off_by_default(tmp_env):
    """The default path (agent/narrator) must not carry explanation payloads."""
    from finance_agent.tools import FinanceFacts

    env = _bundle_env(tmp_env)
    facts = FinanceFacts(env["cfg_path"])
    result = facts.risk_scored_transactions(limit=10, threshold=0.0, focal_only=True)
    rows = result["data"]["rows"]
    assert rows
    # the default payload must not carry explanation noise at all
    assert all("explanation" not in r for r in rows)
    assert result["data"]["explanations_available"] is False


def test_shap_explanations_empty_in_rule_only_mode(tmp_env):
    """Without a bundle there is nothing to explain — no crash, clean payload."""
    from finance_agent.tools import FinanceFacts

    facts = FinanceFacts(tmp_env["cfg_path"])  # bundle_path -> missing.joblib
    assert facts.rule_only()
    result = facts.risk_scored_transactions(
        limit=10, threshold=0.0, include_explanations=True, focal_only=True
    )
    assert result["data"]["explanations_available"] is False
    assert all(r.get("explanation") is None for r in result["data"]["rows"])


def test_risk_scoring_recomputed_once_across_thresholds(tmp_env):
    """2.8: changing the threshold must not recompute rules+features+model."""
    from finance_agent import tools

    tools._scored_frame_json.cache_clear()
    try:
        from finance_agent.tools import FinanceFacts

        facts = FinanceFacts(tmp_env["cfg_path"])
        facts.risk_scored_transactions(limit=10, threshold=0.7)
        facts.risk_scored_transactions(limit=10, threshold=0.2)
        facts.risk_scored_transactions(limit=50, threshold=0.9)
        info = tools._scored_frame_json.cache_info()
        assert info.misses == 1, "expensive path must compute exactly once"
        assert info.hits >= 2
    finally:
        tools._scored_frame_json.cache_clear()
