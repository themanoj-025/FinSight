"""Outbound risk-alert webhook tests (Phase E.3).

Acceptance: with `features.webhook_alerts` + `alerts.webhook_url` configured,
a live risk scan that flags transactions above the threshold POSTs a small
JSON payload; repeated scans do not spam (dedup per transaction); disabling
the flag or leaving the URL empty is a no-op; a dead endpoint never breaks
the scan (log + continue).
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from finance_agent import alerts


class _CaptureHandler(BaseHTTPRequestHandler):
    """Captures POST (path, parsed JSON body) pairs; responds 200."""

    received: list[tuple[str, dict]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).received.append((self.path, body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args) -> None:  # keep test output quiet
        pass


@pytest.fixture(autouse=True)
def _no_webhook_env(monkeypatch) -> None:
    """Tests must be hermetic: a developer's FINSIGHT_WEBHOOK_URL must not leak in."""
    monkeypatch.delenv("FINSIGHT_WEBHOOK_URL", raising=False)


@pytest.fixture()
def webhook_server() -> None:
    server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _CaptureHandler.received = []
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _flagged_data(rows: list[dict] | None = None) -> dict:
    rows = rows or [
        {
            "row_index": 3,
            "date": "2025-02-11",
            "merchant": "M_Pharmacy",
            "amount": 640.0,
            "category": "health",
            "type": "PAYMENT",
            "risk_score": 0.93,
            "rule_score": 0.8,
            "model_score": None,
            "reason": "Unusually large payment to a new payee",
            "fraud_archetype": "new_payee_transfer",
        },
        {
            "row_index": 9,
            "date": "2025-02-15",
            "merchant": "M_Cafe",
            "amount": 12.5,
            "category": "dining",
            "type": "PAYMENT",
            "risk_score": 0.88,
            "rule_score": 0.9,
            "model_score": None,
            "reason": "Duplicate charge in window",
            "fraud_archetype": "duplicate_charge",
        },
    ]
    return {
        "threshold": 0.7,
        "rows": rows,
        "total_scored": 1000,
        "flagged_count": len(rows),
        "scoring_mode": "rule_only",
    }


def _enabled_cfg(webhook_url: str, state_path: str) -> dict:
    return {
        "features": {"webhook_alerts": True},
        "alerts": {"webhook_url": webhook_url, "state_path": state_path},
    }


def test_build_alert_payload_shape_and_cap() -> None:
    many = [{"row_index": i, "merchant": f"M_{i}", "amount": 1.0} for i in range(50)]
    payload = alerts.build_alert_payload(
        _flagged_data(many), source="risk_scan", focal_user="U_Alex"
    )
    assert payload["event"] == "risk_alert"
    assert payload["version"] == 1
    assert payload["source"] == "risk_scan"
    assert payload["focal_user"] == "U_Alex"
    assert payload["threshold"] == 0.7
    assert payload["flagged_count"] == 50
    assert payload["transactions_alerted"] == alerts.MAX_PAYLOAD_ROWS
    assert len(payload["transactions"]) == alerts.MAX_PAYLOAD_ROWS
    # whitelisted keys only — no surprise columns in the payload
    assert set(payload["transactions"][0]) == {"row_index", "merchant", "amount"}
    assert payload["sent_at_utc"]


def test_post_webhook_posts_json(webhook_server) -> None:
    payload = alerts.build_alert_payload(_flagged_data(), focal_user="U_Alex")
    assert alerts.post_webhook(webhook_server, payload) is True
    (path, body) = _CaptureHandler.received[0]
    assert path == "/hook"
    assert body["event"] == "risk_alert"
    assert body["focal_user"] == "U_Alex"
    assert body["transactions"][0]["row_index"] == 3


def test_post_webhook_dead_endpoint_returns_false_not_raise() -> None:
    # Port 1 is virtually never listening — connection refused.
    assert alerts.post_webhook("http://127.0.0.1:1/hook", _flagged_data()) is False


def test_send_risk_alerts_noop_when_disabled_or_unconfigured(webhook_server, tmp_path) -> None:
    cfg = _enabled_cfg(webhook_server, str(tmp_path / "sent.jsonl"))
    cfg["features"]["webhook_alerts"] = False  # flag off
    assert alerts.send_risk_alerts(_flagged_data(), cfg) == 0
    cfg["features"]["webhook_alerts"] = True
    cfg["alerts"]["webhook_url"] = ""  # no URL configured
    assert alerts.send_risk_alerts(_flagged_data(), cfg) == 0
    assert _CaptureHandler.received == []


def test_send_risk_alerts_deduplicates_across_scans(webhook_server, tmp_path) -> None:
    cfg = _enabled_cfg(webhook_server, str(tmp_path / "sent.jsonl"))
    data = _flagged_data()
    assert alerts.send_risk_alerts(data, cfg) == 1  # first scan alerts
    assert alerts.send_risk_alerts(data, cfg) == 0  # same transactions: deduped
    assert len(_CaptureHandler.received) == 1
    assert _CaptureHandler.received[0][1]["transactions_alerted"] == 2


def test_send_risk_alerts_dead_endpoint_never_raises(tmp_path) -> None:
    cfg = _enabled_cfg("http://127.0.0.1:1/hook", str(tmp_path / "sent.jsonl"))
    assert alerts.send_risk_alerts(_flagged_data(), cfg) == 0  # delivery failed, logged
    # nothing was recorded as sent, so a future scan may retry
    assert not (tmp_path / "sent.jsonl").exists()


def test_send_risk_alerts_concurrent_scans_fire_once(webhook_server, tmp_path) -> None:
    """Two threads scanning the same ledger must not double-fire the webhook.

    The facts layer serves both the threaded FastAPI server and the agent, so
    the read-check-POST-append sequence is serialized by a module lock: the
    second scan must see the first scan's persisted dedup state, not race past
    it. Uses the live server so the first POST succeeds and persists state
    before the second thread reads it.
    """
    import threading
    from unittest.mock import patch

    cfg = _enabled_cfg(webhook_server, str(tmp_path / "sent.jsonl"))
    data = _flagged_data()

    with patch.object(alerts, "post_webhook", wraps=alerts.post_webhook) as post:
        results: list[int] = []

        def _scan() -> None:
            results.append(alerts.send_risk_alerts(data, cfg))

        threads = [threading.Thread(target=_scan) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert post.call_count == 1, "concurrent scans must dedup, not double-fire"
        assert sorted(results) == [0, 1]


def test_send_risk_alerts_state_pruned_and_reflagged_alerted(webhook_server, tmp_path) -> None:
    """State stays bounded; a flag that drops and later re-flags alerts anew."""
    cfg = _enabled_cfg(webhook_server, str(tmp_path / "sent.jsonl"))
    data = _flagged_data()  # rows 3 and 9 flagged
    assert alerts.send_risk_alerts(data, cfg) == 1
    assert len(_CaptureHandler.received) == 1

    # Only row 9 remains flagged: state is pruned to {9}.
    data_9 = dict(data, rows=[data["rows"][1]], flagged_count=1)
    assert alerts.send_risk_alerts(data_9, cfg) == 0  # nothing new
    state = {
        int(json.loads(line)["row_index"])
        for line in (tmp_path / "sent.jsonl").read_text().splitlines()
    }
    assert state == {9}

    # Row 3 re-flags later: it was pruned, so this is a new episode -> alert.
    data_3 = dict(data, rows=[data["rows"][0]], flagged_count=1)
    assert alerts.send_risk_alerts(data_3, cfg) == 1
    assert len(_CaptureHandler.received) == 2


def test_risk_scan_posts_webhook_for_flagged_transactions(webhook_server, tmp_path) -> None:
    """End-to-end: a live scan on real (tiny) data flags fraud -> one POST."""
    import yaml

    from finance_agent.tools import FinanceFacts
    from generate_data import generate

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
        "features": {"webhook_alerts": True},
        "alerts": {"webhook_url": webhook_server, "state_path": str(tmp_path / "sent.jsonl")},
        "risk": {
            "blend": {"rules": 0.4, "supervised": 0.3, "isolation_forest": 0.3},
            "fraud_threshold": 0.7,
        },
        "agent": {"model": "claude-sonnet-4-5", "max_turns": 3},
    }
    cfg_path = tmp_path / "config.yaml"
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)

    facts = FinanceFacts(str(cfg_path))
    result = facts.risk_scored_transactions(limit=15)
    assert result["data"]["flagged_count"] > 0, "fixture fraud must flag above the threshold"

    assert len(_CaptureHandler.received) == 1, "first scan must POST exactly one alert"
    payload = _CaptureHandler.received[0][1]
    assert payload["event"] == "risk_alert"
    assert payload["flagged_count"] == result["data"]["flagged_count"]
    assert payload["transactions"], "payload must carry the flagged rows"
    assert "row_index" in payload["transactions"][0]

    # A second scan of the same ledger must NOT re-alert (dedup by row_index).
    facts.risk_scored_transactions(limit=15)
    assert len(_CaptureHandler.received) == 1
