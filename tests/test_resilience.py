"""Graceful-degradation resilience suite (Phase D.3).

Deliberately break each external dependency in turn — missing/corrupt/tampered
model bundle, missing API key, malformed config, missing data, dead LLM
client — and assert the system degrades **visibly and correctly** instead of
crashing or, worse, silently returning wrong answers. This is the regression
net for the whole failure class behind the original "rule-only silently always
clean" bug from the first audit in this series.
"""

import logging

import joblib
import pytest
import yaml

from finance_agent.config_schema import ConfigError


def _write_config(tmp_path, df, *, bundle="missing.joblib", extra: dict | None = None) -> str:

    data_path = tmp_path / "transactions.csv"
    df.to_csv(data_path, index=False)
    cfg = {
        "data": {"path": str(data_path)},
        "model_bench": {"bundle_path": str(tmp_path / bundle)},
        "risk": {
            "blend": {"rules": 0.4, "supervised": 0.3, "isolation_forest": 0.3},
            "fraud_threshold": 0.7,
        },
        "agent": {"activity_log": str(tmp_path / "activity.jsonl"), "model": "claude-sonnet-4-5"},
    }
    if extra:
        cfg.update(extra)
    cfg_path = tmp_path / "config.yaml"
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)
    return str(cfg_path)


@pytest.fixture()
def df():
    from generate_data import generate

    return generate(
        days=30, seed=7, user="U_Alex", n_background_accounts=10, start_date="2025-01-01"
    )


# ------------------------------------------------------------ missing bundle
def test_missing_bundle_degrades_to_rule_only(tmp_path, df):
    from finance_agent.tools import FinanceFacts

    cfg_path = _write_config(tmp_path, df, bundle="does_not_exist.joblib")
    facts = FinanceFacts(cfg_path)
    assert facts.rule_only() is True
    result = facts.risk_scored_transactions(limit=5)
    assert result["data"]["scoring_mode"] == "rule_only"
    assert result["data"]["total_scored"] == len(df)  # scoring still works


# -------------------------------------------------- corrupt / tampered bundle
def test_tampered_signed_bundle_is_refused(tmp_path, df, monkeypatch):
    """A bundle whose signature no longer matches must NOT be deserialized —
    the pickle-RCE mitigation (C.2.4) fails closed to rule-only."""
    from finance_agent.bundle_security import write_signature
    from finance_agent.tools import FinanceFacts

    bundle = tmp_path / "bundle.joblib"
    joblib.dump(
        {
            "best_model": None,
            "isolation_forest": None,
            "scaler": None,
            "feature_names": [],
            "needs_scaling": False,
            "metrics": {},
        },
        str(bundle),
    )
    write_signature(str(bundle))
    cfg_path = _write_config(tmp_path, df, bundle="bundle.joblib")

    facts = FinanceFacts(cfg_path)
    assert facts.rule_only() is False  # signed bundle loads fine

    # tamper with the bundle bytes after signing
    with open(bundle, "ab") as fh:
        fh.write(b"\x00tampered")
    facts2 = FinanceFacts(cfg_path)
    assert facts2.rule_only() is True, "tampered bundle must be refused (fail closed)"


def test_unsigned_bundle_refused_when_env_key_set(tmp_path, df, monkeypatch):
    """With FINSIGHT_BUNDLE_KEY set, a bundle with no .sig must be refused —
    the guarantee can't be silently downgraded."""
    from finance_agent.tools import FinanceFacts

    monkeypatch.setenv("FINSIGHT_BUNDLE_KEY", "super-secret-test-key")
    bundle = tmp_path / "bundle.joblib"
    joblib.dump({"not": "signed"}, str(bundle))  # deliberately no .sig
    cfg_path = _write_config(tmp_path, df, bundle="bundle.joblib")

    facts = FinanceFacts(cfg_path)
    assert facts.rule_only() is True


def test_demo_key_signed_bundle_rejected_under_real_key(tmp_path, df, monkeypatch):
    """Audit §2: a bundle signed with the public demo default key must be
    REFUSED loudly when FINSIGHT_BUNDLE_KEY is set to a different, real value.

    The demo default is public (it ships in this repo), so anyone can sign a
    bundle with it — which is exactly why a production deployment that sets a
    real key must never silently accept one. The fail-fast startup guard
    (:func:`ensure_bundle_verified`) raises a specific error naming the fix,
    instead of the rule-only fallback masking the misconfiguration.
    """
    from finance_agent import bundle_security

    bundle = tmp_path / "bundle.joblib"
    joblib.dump(
        {
            "best_model": None,
            "isolation_forest": None,
            "scaler": None,
            "feature_names": [],
            "needs_scaling": False,
            "metrics": {},
        },
        str(bundle),
    )
    # Signed while env key is unset -> the public demo default is used.
    bundle_security.write_signature(str(bundle))
    assert bundle_security.key_origin() == "demo-default"
    assert bundle_security.verify_bundle(str(bundle))[0] is True  # demo key matches

    monkeypatch.setenv("FINSIGHT_BUNDLE_KEY", "real-production-secret")
    assert bundle_security.key_origin() == "env"
    # Under the real key the same bundle is a mismatch — never silently OK:
    assert bundle_security.verify_bundle(str(bundle))[0] is False
    with pytest.raises(bundle_security.BundleSignatureError) as excinfo:
        bundle_security.ensure_bundle_verified(str(bundle))
    message = str(excinfo.value)
    assert "signature" in message.lower()
    assert "make train" in message  # the error points at the re-sign fix
    assert "FINSIGHT_BUNDLE_KEY" in message


def test_corrupt_unsigned_bundle_falls_back_to_rule_only(tmp_path, df, caplog):
    """Garbage bytes without a signature: joblib.load fails, caught, rule-only."""
    from finance_agent.tools import FinanceFacts

    bundle = tmp_path / "bundle.joblib"
    with open(bundle, "wb") as fh:
        fh.write(b"this is not a pickle \x00\xff")
    cfg_path = _write_config(tmp_path, df, bundle="bundle.joblib")
    with caplog.at_level(logging.WARNING):
        facts = FinanceFacts(cfg_path)
    assert facts.rule_only() is True
    assert any("rule-only" in r.message for r in caplog.records)


# --------------------------------------------------------------- missing key
def test_no_api_key_degrades_to_offline_narrator(tmp_path, df):
    from finance_agent.agent import FinanceAgent

    cfg_path = _write_config(tmp_path, df)
    agent = FinanceAgent(cfg_path, api_key="")
    assert agent.llm_available() is False
    reply = str(agent.answer("How can I save more?"))
    assert reply.strip()  # narrator produces a real answer
    assert agent.usage_summary()["narrator_calls"] >= 1


def test_dead_llm_client_falls_back_to_narrator(tmp_path, df):
    """An LLM call that raises mid-stream must degrade to the narrator, and
    the failure must be visible in the usage ledger (not silent)."""
    from finance_agent.agent import FinanceAgent

    class BoomStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @property
        def text_stream(self):
            raise RuntimeError("api down")

    class BoomMessages:
        def stream(self, **kwargs):
            return BoomStream()

    class BoomClient:
        models = type("M", (), {"list": lambda self, limit=1: {"data": []}})()
        messages = BoomMessages()

    class BoomAnthropic:
        def Anthropic(self, api_key=""):
            return BoomClient()

    cfg_path = _write_config(tmp_path, df)
    agent = FinanceAgent(cfg_path, api_key="sk-x", _anthropic=BoomAnthropic())
    reply = str(agent.answer("Any suspicious activity?"))
    assert "offline fallback" in reply
    totals = agent.usage_summary()
    assert totals["failed_calls"] == 1  # the failure is recorded, not hidden


# ----------------------------------------------------------- malformed config
def test_malformed_config_raises_config_error(tmp_path, df):
    """A typo'd config must fail loudly at load with the offending key named."""
    from finance_agent.tools import FinanceFacts

    cfg_path = _write_config(
        tmp_path,
        df,
        extra={"risk": {"blend": {"rules": 0.9, "supervised": 0.9, "isolation_forest": 0.9}}},
    )
    with pytest.raises(ConfigError):
        FinanceFacts(cfg_path)


# --------------------------------------------------------------- missing data
def test_missing_data_raises_clear_file_not_found_error(tmp_path, df):
    """Missing data must surface as a clear FileNotFoundError, not an opaque
    crash (the app wraps this with an explicit 'run make data' message)."""
    from finance_agent.tools import FinanceFacts

    cfg_path = tmp_path / "config.yaml"
    cfg = {
        "data": {"path": str(tmp_path / "nope.csv")},
        "model_bench": {"bundle_path": str(tmp_path / "nope.joblib")},
        "risk": {
            "blend": {"rules": 0.4, "supervised": 0.3, "isolation_forest": 0.3},
            "fraud_threshold": 0.7,
        },
        "agent": {"activity_log": str(tmp_path / "activity.jsonl"), "model": "claude-sonnet-4-5"},
    }
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)
    with pytest.raises(FileNotFoundError):
        FinanceFacts(str(cfg_path))


# ------------------------------------------------------- retrieval resilience
def test_retrieval_disabled_flag_returns_clean_payload(tmp_path, df):
    """The similar-transactions tool must return a clean 'disabled' payload
    when the feature flag is off — never raise."""
    from finance_agent.tools import FinanceFacts

    cfg_path = _write_config(tmp_path, df, extra={"features": {"faiss_retrieval": False}})
    facts = FinanceFacts(cfg_path)
    result = facts.find_similar_transactions(k=3)
    assert result["data"]["enabled"] is False
    assert result["data"]["neighbors"] == []
