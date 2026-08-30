"""Config validation tests (Phase 2.4): malformed config raises a ConfigError
naming the offending key — never a bare KeyError three calls deep."""

import copy

import pytest

from finance_agent.config_schema import ConfigError, validate_config

BASE = {
    "data": {"path": "data/transactions.csv"},
    "model_bench": {
        "bundle_path": "model_bench/risk_model_bundle.joblib",
        "best_model_path": "model_bench/best_model.joblib",
        "metadata_path": "model_bench/best_model_metadata.json",
        "artifacts_dir": "model_bench/results",
    },
    "risk": {
        "blend": {"rules": 0.4, "supervised": 0.3, "isolation_forest": 0.3},
        "fraud_threshold": 0.7,
    },
    "agent": {"model": "claude-sonnet-4-5", "activity_log": "data/agent_activity.jsonl"},
}


def test_valid_config_passes() -> None:
    cfg = validate_config(copy.deepcopy(BASE))
    assert cfg["risk"]["fraud_threshold"] == 0.7


def test_blend_must_sum_to_one() -> None:
    cfg = copy.deepcopy(BASE)
    cfg["risk"]["blend"]["rules"] = 0.5  # now 0.5 + 0.3 + 0.3 = 1.1
    with pytest.raises(ConfigError, match="sum to 1.0"):
        validate_config(cfg)


def test_blend_weight_out_of_range() -> None:
    cfg = copy.deepcopy(BASE)
    cfg["risk"]["blend"]["supervised"] = 1.5
    with pytest.raises(ConfigError, match="\\[0, 1\\]"):
        validate_config(cfg)


def test_threshold_out_of_range_names_key() -> None:
    cfg = copy.deepcopy(BASE)
    cfg["risk"]["fraud_threshold"] = 1.7
    with pytest.raises(ConfigError, match="fraud_threshold"):
        validate_config(cfg)


def test_missing_agent_model_named() -> None:
    cfg = copy.deepcopy(BASE)
    del cfg["agent"]["model"]
    with pytest.raises(ConfigError, match="agent.model"):
        validate_config(cfg)


def test_path_traversal_rejected() -> None:
    cfg = copy.deepcopy(BASE)
    cfg["data"]["path"] = "../../etc/passwd"
    with pytest.raises(ConfigError, match="data.path"):
        validate_config(cfg)


def test_empty_path_rejected() -> None:
    cfg = copy.deepcopy(BASE)
    cfg["model_bench"]["bundle_path"] = ""
    with pytest.raises(ConfigError, match="bundle_path"):
        validate_config(cfg)


def test_focal_user_must_be_in_focal_users() -> None:
    cfg = copy.deepcopy(BASE)
    cfg["data"]["focal_user"] = "U_Alex"
    cfg["data"]["focal_users"] = ["U_Maria"]
    with pytest.raises(ConfigError, match="data.focal_user"):
        validate_config(cfg)


def test_focal_users_rejects_non_string_entries() -> None:
    cfg = copy.deepcopy(BASE)
    cfg["data"]["focal_users"] = ["U_Alex", 123]
    with pytest.raises(ConfigError, match="focal_users"):
        validate_config(cfg)
