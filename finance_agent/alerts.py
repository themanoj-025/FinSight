"""Outbound risk-alert webhooks (Phase E.3).

When ``features.webhook_alerts`` is enabled and ``alerts.webhook_url`` (or the
``FINSIGHT_WEBHOOK_URL`` env var) is configured, a live risk scan that flags
transactions above the configured threshold POSTs a small JSON payload to the
endpoint — e.g. a Slack Incoming Webhook or any HTTP receiver. This is the
"how would this integrate into a real workflow" answer: an opt-in push out of
the demo, not an event-streaming platform.

Design rules:

* **Stdlib-only** — ``urllib`` POST, mirroring the digest module's Slack
  delivery pattern. No new dependency.
* **Never raises** — this fires inside the hot risk-scan path
  (``FinanceFacts.risk_scored_transactions``); a webhook outage, a dead
  endpoint, or an unwritable dedup file must degrade to a log line, never
  break the scan.
* **Deduplicated per flag episode** — each flagged transaction is alerted at
  most once while it stays flagged (state in ``alerts.state_path``, default
  ``data/risk_alerts_sent.jsonl``, keyed by ``row_index``), so page reloads /
  repeated scans / agent turns don't spam the endpoint. The state is rewritten
  to the currently-flagged set on every scan, so it stays bounded and a
  transaction that drops below the threshold and later rises again is a *new*
  episode that alerts anew. Regenerated data has fresh row indexes, so a new
  ledger re-alerts naturally.
* **Secret-safe logging** — webhook URLs embed a token in the path (Slack
  style); logs print only ``scheme://host``, never the full URL.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("finance_agent.alerts")

# Serializes the read-check-POST-append sequence. The facts layer serves both
# the (threaded) FastAPI server and the agent, so two concurrent scans must not
# both read "nothing sent" and double-fire the endpoint.
_STATE_LOCK = threading.Lock()

DEFAULT_STATE_PATH = "data/risk_alerts_sent.jsonl"
MAX_PAYLOAD_ROWS = 25

log = logging.getLogger("finance_agent.alerts")

DEFAULT_STATE_PATH = "data/risk_alerts_sent.jsonl"
MAX_PAYLOAD_ROWS = 25

# Whitelisted per-transaction fields for the payload — a receiver gets the
# explainable essentials, never every scored column.
_ROW_KEYS = (
    "row_index",
    "date",
    "merchant",
    "amount",
    "category",
    "type",
    "risk_score",
    "rule_score",
    "model_score",
    "reason",
    "fraud_archetype",
)


def webhook_url(cfg: dict[str, Any]) -> str:
    """Configured webhook URL — the env var wins over config.

    ``FINSIGHT_WEBHOOK_URL`` (like ``DIGEST_SLACK_WEBHOOK``) keeps the secret
    out of the committed config file; the config value is the fallback.
    """
    return (
        os.environ.get("FINSIGHT_WEBHOOK_URL", "").strip()
        or str((cfg.get("alerts") or {}).get("webhook_url", "")).strip()
    )


def webhook_enabled(cfg: dict[str, Any]) -> bool:
    """True only when BOTH the feature flag and a URL are configured."""
    return bool((cfg.get("features") or {}).get("webhook_alerts")) and bool(webhook_url(cfg))


def _safe_url(url: str) -> str:
    """``scheme://host`` only — webhook paths embed secrets and must not log."""
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def build_alert_payload(
    data: dict[str, Any],
    *,
    source: str = "risk_scan",
    focal_user: str | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The small JSON payload for one alert event.

    ``data`` is the ``risk_scored_transactions`` payload (``threshold``,
    ``rows``, ``flagged_count``, ``total_scored``, ...). ``rows`` overrides
    ``data["rows"]`` when the caller sends only the newly-flagged subset; the
    payload is capped at ``MAX_PAYLOAD_ROWS`` transactions to stay small even
    when a scan flags hundreds.
    """
    shown = rows if rows is not None else (data.get("rows") or [])
    transactions = [{k: r.get(k) for k in _ROW_KEYS if k in r} for r in shown[:MAX_PAYLOAD_ROWS]]
    return {
        "event": "risk_alert",
        "version": 1,
        "source": source,
        "sent_at_utc": datetime.now(timezone.utc).isoformat(),
        "threshold": data.get("threshold"),
        "flagged_count": data.get("flagged_count", len(shown)),
        "total_scored": data.get("total_scored"),
        "scoring_mode": data.get("scoring_mode"),
        "focal_user": focal_user,
        "transactions_alerted": len(transactions),
        "transactions": transactions,
    }


def post_webhook(url: str, payload: dict[str, Any], *, timeout: float = 15.0) -> bool:
    """POST the payload to the webhook URL; returns True on HTTP success.

    Never raises: transport errors, non-2xx responses, and bad payloads are
    logged (with the secret-safe URL) and reported as ``False``.
    """
    body = json.dumps(payload, default=str).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(
            req, timeout=timeout
        ) as resp:  # noqa: S310 — user-configured webhook
            if resp.status >= 400:
                log.warning("Webhook %s returned HTTP %s", _safe_url(url), resp.status)
                return False
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        log.warning("Webhook delivery to %s failed: %s", _safe_url(url), exc)
        return False
    return True


def _load_sent_ids(state_path: str) -> set[int]:
    """row_index values already alerted (corrupt lines are skipped, not fatal)."""
    sent: set[int] = set()
    path = Path(state_path)
    if not path.exists():
        return sent
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            sent.add(int(json.loads(line)["row_index"]))
        except (ValueError, KeyError, TypeError):
            continue  # a corrupt line must not block alerting
    return sent


def _rewrite_sent(state_path: str, keep: set[int]) -> None:
    """Persist the dedup state as the set of row indexes to keep.

    The state is rewritten (not appended) on every alert so it stays bounded to
    the currently-flagged set: a transaction that drops below the threshold and
    later rises again is a *new* flag episode and alerts anew, and the file
    cannot grow without bound across months of scans. An unwritable state file
    is logged, not fatal (dedup degrades to "may re-alert", never raises).
    """
    try:
        path = Path(state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        with open(path, "w", encoding="utf-8") as fh:
            for row_index in sorted(keep):
                fh.write(json.dumps({"row_index": row_index, "sent_at_utc": now}) + "\n")
    except OSError as exc:
        log.warning(
            "Could not record alert state %s: %s — a future scan may re-alert.", state_path, exc
        )


def send_risk_alerts(
    data: dict[str, Any],
    cfg: dict[str, Any],
    *,
    source: str = "risk_scan",
    focal_user: str | None = None,
    state_path: str | None = None,
) -> int:
    """Send one deduplicated alert for newly-flagged transactions.

    Returns the number of webhook POSTs made (0 when disabled, unconfigured,
    nothing new to report, or delivery failed). Never raises — the risk scan
    that calls this is the contract, the webhook is best-effort.
    """
    if not webhook_enabled(cfg):
        return 0
    url = webhook_url(cfg)
    state_path = state_path or str(
        (cfg.get("alerts") or {}).get("state_path") or DEFAULT_STATE_PATH
    )

    rows = data.get("rows") or []
    if not rows or not data.get("flagged_count", 0):
        return 0

    with _STATE_LOCK:
        sent = _load_sent_ids(state_path)
        current = {int(r.get("row_index", -1)) for r in rows}
        new_rows = [r for r in rows if int(r.get("row_index", -1)) not in sent]
        if not new_rows:
            # Nothing new, but prune flags that dropped out so a future re-flag
            # is a *new* episode and alerts anew (and the file stays bounded).
            if current != sent:
                _rewrite_sent(state_path, current)
            return 0

        payload = build_alert_payload(data, source=source, focal_user=focal_user, rows=new_rows)
        if not post_webhook(url, payload):
            return 0
        # State stays bounded to what is currently flagged: newly-sent rows are
        # recorded and rows that dropped below the threshold are pruned.
        _rewrite_sent(state_path, current)
        log.info(
            "Webhook alert sent: %d flagged transaction(s) to %s",
            len(new_rows),
            _safe_url(url),
        )
    return 1
