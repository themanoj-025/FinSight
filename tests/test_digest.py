"""Tests for the weekly digest (finance_agent/digest.py, stretch feature #3).

Covers: digest markdown content sections, Slack webhook delivery (mocked, no
network), SMTP email delivery (mocked, no network), graceful skip when no
channel is configured, and the CLI `digest` subcommand wiring.
"""

import logging
from unittest import mock

import pytest

from finance_agent.digest import build_weekly_digest, run_digest, send_email, send_slack


@pytest.fixture()
def digest_env(tmp_path):
    """Hermetic env: small multi-user ledger + config with digest settings."""
    import yaml

    from generate_data import generate

    df = generate(
        days=30,
        seed=7,
        focal_users=["U_Alex", "U_Maria"],
        n_background_accounts=15,
        n_fraud_pairs=2,
        start_date="2025-01-01",
    )
    data_path = tmp_path / "transactions.csv"
    df.to_csv(data_path, index=False)
    cfg = {
        "data": {"path": str(data_path), "focal_users": ["U_Alex", "U_Maria"]},
        "model_bench": {"bundle_path": str(tmp_path / "missing.joblib")},
        "risk": {
            "blend": {"rules": 0.4, "supervised": 0.3, "isolation_forest": 0.3},
            "fraud_threshold": 0.7,
        },
        "budgets": {"monthly": {"dining": 1000.0}},
        "digest": {
            "out_path": str(tmp_path / "weekly_digest.md"),
            "slack_webhook": "https://hooks.slack.com/services/TEST",
            "email": {
                "smtp_host": "smtp.test.local",
                "smtp_port": 587,
                "smtp_user": "user@test.local",
                "smtp_password": "secret",
                "from_addr": "finsight@test.local",
                "to_addrs": ["me@test.local"],
            },
        },
        "agent": {"activity_log": str(tmp_path / "activity.jsonl"), "model": "claude-sonnet-4-5"},
    }
    cfg_path = tmp_path / "config.yaml"
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)
    return {"cfg_path": str(cfg_path), "tmp": tmp_path}


def _facts(digest_env):
    from finance_agent.tools import FinanceFacts

    return FinanceFacts(digest_env["cfg_path"])


def test_build_weekly_digest_has_expected_sections(digest_env):
    md = build_weekly_digest(_facts(digest_env))
    assert md.startswith("# 💸 FinSight Weekly Digest")
    for section in ("The week at a glance", "Health score", "window"):
        assert section in md
    assert "No transaction data" not in md


def test_build_weekly_digest_scoped_to_focal_user(digest_env):
    """Digest figures reflect only the selected focal user's ledger."""
    from finance_agent.tools import FinanceFacts

    alex = build_weekly_digest(FinanceFacts(digest_env["cfg_path"], focal_user="U_Alex"))
    maria = build_weekly_digest(FinanceFacts(digest_env["cfg_path"], focal_user="U_Maria"))
    # Income appears (salary rows) — the weekly glance line has real numbers.
    assert "**Income:**" in alex and "**Income:**" in maria
    assert "$" in alex
    # The two users have distinct ledgers (merchants, anomalies, spend), so the
    # digests must differ — proving the digest is not merging all accounts.
    assert alex != maria


def test_build_weekly_digest_empty_data(tmp_path):
    """A facts source with no rows must not crash the digest."""
    import pandas as pd
    import yaml

    from finance_agent.tools import FinanceFacts

    empty = tmp_path / "empty.csv"
    pd.DataFrame(
        columns=[
            "step",
            "type",
            "amount",
            "nameOrig",
            "oldbalanceOrg",
            "newbalanceOrig",
            "nameDest",
            "oldbalanceDest",
            "newbalanceDest",
            "merchant",
            "category",
            "datetime",
            "date",
            "is_focal_user",
            "isFraud",
            "isFlaggedFraud",
            "is_anomaly",
            "anomaly_type",
        ]
    ).to_csv(empty, index=False)
    cfg = {
        "data": {"path": str(empty)},
        "risk": {
            "blend": {"rules": 0.4, "supervised": 0.3, "isolation_forest": 0.3},
            "fraud_threshold": 0.7,
        },
        "agent": {"model": "claude-sonnet-4-5", "activity_log": str(tmp_path / "a.jsonl")},
        "model_bench": {"bundle_path": str(tmp_path / "missing.joblib")},
    }
    cfg_path = tmp_path / "config.yaml"
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)
    facts = FinanceFacts(str(cfg_path))
    md = build_weekly_digest(facts)
    assert "No transaction data" in md


def test_send_slack_posts_json_payload():
    """send_slack POSTs {"text": ...} to the webhook and surfaces HTTP errors."""
    with mock.patch("urllib.request.urlopen") as mock_open:
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_open.return_value.__enter__.return_value = mock_resp
        send_slack("https://hooks.slack.com/services/X", "hello digest")
        req = mock_open.call_args[0][0]
        assert req.full_url == "https://hooks.slack.com/services/X"
        assert req.data == b'{"text": "hello digest"}'

    with (
        mock.patch("urllib.request.urlopen", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        send_slack("https://hooks.slack.com/services/X", "x")


def test_send_email_uses_tls_and_login():
    """send_email goes through SMTP with starttls + optional login."""
    cfg = {
        "smtp_host": "smtp.test.local",
        "smtp_port": 587,
        "smtp_user": "user@test.local",
        "smtp_password": "pw",
        "from_addr": "from@test.local",
        "to_addrs": ["a@test.local", "b@test.local"],
    }
    with mock.patch("smtplib.SMTP") as mock_smtp:
        server = mock_smtp.return_value.__enter__.return_value
        send_email(cfg, "Subject line", "Body text")
        mock_smtp.assert_called_once_with("smtp.test.local", 587, timeout=15)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("user@test.local", "pw")
        server.send_message.assert_called_once()
        msg = server.send_message.call_args[0][0]
        assert msg["Subject"] == "Subject line"
        assert msg["To"] == "a@test.local, b@test.local"


def test_run_digest_writes_file_and_delivers(digest_env, caplog):
    """run_digest persists the digest and calls both configured channels."""
    caplog.set_level(logging.INFO, logger="finance_agent.digest")
    with (
        mock.patch("finance_agent.digest.send_slack") as mock_slack,
        mock.patch("finance_agent.digest.send_email") as mock_email,
    ):
        md = run_digest(digest_env["cfg_path"])
    out = digest_env["tmp"] / "weekly_digest.md"
    assert out.exists()
    assert out.read_text(encoding="utf-8") == md
    mock_slack.assert_called_once()
    mock_email.assert_called_once()
    assert "Posted digest to Slack webhook" in caplog.text
    assert "Emailed digest to" in caplog.text


def test_run_digest_no_channels_writes_file_only(digest_env, caplog):
    """With no channel configured the digest is written with a visible note."""
    import logging

    import yaml

    caplog.set_level(logging.INFO, logger="finance_agent.digest")
    cfg_path = digest_env["cfg_path"]
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg["digest"] = {"out_path": str(digest_env["tmp"] / "weekly.md")}
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)

    with (
        mock.patch("finance_agent.digest.send_slack") as mock_slack,
        mock.patch("finance_agent.digest.send_email") as mock_email,
    ):
        run_digest(cfg_path)
    mock_slack.assert_not_called()
    mock_email.assert_not_called()
    assert "wrote file only" in caplog.text


def test_run_digest_rejects_path_traversal(digest_env):
    """An explicit out_path may not traverse outside the project."""
    with pytest.raises(ValueError, match="must not traverse"):
        run_digest(digest_env["cfg_path"], out_path="../evil.md")


def test_cli_digest_subcommand(digest_env, capsys, monkeypatch):
    """`python -m finance_agent digest` routes to run_digest, writes the file,
    and prints a confirmation (the CLI must not be silent)."""
    import sys

    out_path = digest_env["tmp"] / "cli.md"
    monkeypatch.setattr(sys, "argv", ["finance_agent", "digest", "--out", str(out_path)])
    with (
        mock.patch("finance_agent.digest.send_slack"),
        mock.patch("finance_agent.digest.send_email"),
    ):
        from finance_agent.cli import main

        assert main() == 0
    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8").startswith("# 💸 FinSight Weekly Digest")
    assert "Weekly digest built" in capsys.readouterr().out
