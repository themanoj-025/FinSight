"""Weekly digest — a scheduled, self-contained summary delivered to Slack or email.

Runs as a scheduled job (`.github/workflows/digest.yml` weekly cron, or
`make digest` locally) and produces a compact Markdown digest of the last 7
days: monthly summary, top spending categories, budget tracker, recurring
payments, fraud-scan highlights, health score, and tips. Every figure comes
from the facts layer, so the digest is exactly as honest as the app.

Delivery is **opt-in and stdlib-only** (no new dependencies):

  * Slack — `DIGEST_SLACK_WEBHOOK` env var (or config `digest.slack_webhook`).
    Uses `urllib` to POST `{"text": ...}` to the Incoming Webhook URL.
  * Email — SMTP settings in config `digest.email` (host/port/user/password
    from env `DIGEST_SMTP_PASSWORD`), stdlib `smtplib` + `email.message`.

Both channels are skipped with a visible note when unconfigured — the digest
itself is always written to `digest.out_path` (default `reports/weekly_digest.md`).

The task: ``python -m finance_agent digest`` or ``make digest``.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pandas as pd

from finance_agent.constants import fmt_money
from finance_agent.report import FactsSource
from finance_agent.rules import expense_rows

log = logging.getLogger("finance_agent.digest")


def _slack_webhook(cfg: dict[str, Any]) -> str:
    return (
        os.environ.get("DIGEST_SLACK_WEBHOOK", "").strip()
        or str(cfg.get("digest", {}).get("slack_webhook", "")).strip()
    )


def _email_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    email = cfg.get("digest", {}).get("email") or {}
    return {
        "smtp_host": str(email.get("smtp_host", "")).strip(),
        "smtp_port": int(email.get("smtp_port", 587)),
        "smtp_user": str(email.get("smtp_user", "")).strip(),
        # Secrets come only from env vars — a password in config.yaml would be
        # a footgun for anyone committing the file (see config.yaml comment).
        "smtp_password": os.environ.get("DIGEST_SMTP_PASSWORD", "").strip(),
        "from_addr": str(email.get("from_addr", "")).strip(),
        "to_addrs": [str(a).strip() for a in email.get("to_addrs", []) if str(a).strip()],
    }


def _digest_window(d: pd.DataFrame) -> tuple[date | None, date | None]:
    """(start, end) for the digest's glance line over the focal user's ledger.

    Defaults to the trailing 7 days of the ledger. When that window contains
    **no income** but a salary deposit exists within the trailing ~35 days,
    the start extends back to the most recent payday — so a weekly recap
    never silently reports "income: $0" for a monthly-salary user, and it
    still covers the most recent activity (no stale-week blind spot). The
    exact window is printed in the digest header, so every figure is scoped
    to what is shown.
    """
    if d.empty:
        return None, None
    dates = pd.to_datetime(d["date"])
    end = dates.max().date()
    start = end - timedelta(days=6)
    window_has_income = bool(
        d.loc[dates.dt.date >= start, "type"].isin({"SALARY", "CASH_IN"}).any()
    )
    if not window_has_income:
        salary_dates = pd.to_datetime(d.loc[d["type"] == "SALARY", "date"]).dt.date
        if not salary_dates.empty:
            last_salary = salary_dates.max()
            if last_salary >= end - timedelta(days=35):
                start = min(start, last_salary)
    return start, end


def build_weekly_digest(facts: FactsSource) -> str:
    """A compact Markdown digest of the trailing 7 days of the ledger."""
    d = facts._focal().copy()  # noqa: SLF001 — both FinanceFacts and ApiClient expose it
    start_dt, end_dt = _digest_window(d)
    if start_dt is None or end_dt is None:
        return "# Weekly Digest\n\nNo transaction data available yet.\n"
    # The facts tools are month-scoped; the weekly view is a filtered slice of
    # the selected focal user's ledger computed directly from the source frame.
    d["_d"] = pd.to_datetime(d["date"]).dt.date
    window = d[(d["_d"] >= start_dt) & (d["_d"] <= end_dt)]

    income = float(window.loc[window["type"].isin({"SALARY", "CASH_IN"}), "amount"].sum())
    expenses = float(window.loc[expense_rows(window), "amount"].sum())
    net = income - expenses
    health = facts.financial_health()["data"]
    tips = facts.top_tips()["data"]["tips"]
    risk = facts.risk_scored_transactions(limit=5)

    out: list[str] = []
    out.append("# 💸 FinSight Weekly Digest")
    out.append("")
    out.append(
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"window {start_dt.isoformat()} → {end_dt.isoformat()}_"
    )
    out.append("")
    out.append("## The week at a glance")
    out.append("")
    out.append(
        f"- **Income:** {fmt_money(income)}  ·  **Spending:** {fmt_money(expenses)}  "
        f"·  **Net:** {fmt_money(net)}"
    )
    out.append(
        f"- **Health score:** {health['score']}/100 · savings rate "
        f"{health['savings_rate'] * 100:.1f}% · buffer "
        f"~{health['buffer_months']} months"
    )
    out.append("")

    breakdown = facts.category_breakdown()
    rows = breakdown["data"].get("rows", [])
    if rows:
        out.append("## Top spending categories")
        out.append("")
        out.append("| Category | Amount | Share |")
        out.append("|---|---|---|")
        for r in rows[:6]:
            out.append(f"| {r['category']} | {fmt_money(r['amount'])} | {r['share'] * 100:.1f}% |")
        out.append("")

    budgets = facts.budget_status()
    if budgets["data"].get("configured") and budgets["data"].get("rows"):
        over = [r for r in budgets["data"]["rows"] if r["over"]]
        if over:
            out.append("## ⚠️ Over budget")
            out.append("")
            out.append(
                ", ".join(
                    f"**{r['category']}** ({r['pct'] * 100:.0f}% of {fmt_money(r['goal'])})"
                    for r in over
                )
            )
            out.append("")

    rec = facts.recurring_payments()
    if rec["data"].get("rows"):
        out.append("## Recurring payments")
        out.append("")
        out.append(
            ", ".join(
                f"{r['merchant']} {fmt_money(r['amount'])}/mo" for r in rec["data"]["rows"][:6]
            )
        )
        out.append("")

    flagged = [
        r for r in risk["data"].get("rows", []) if r["risk_score"] >= risk["data"]["threshold"]
    ]
    if flagged:
        out.append("## 🛡️ Risk scan highlights")
        out.append("")
        out.append(f"{risk['summary']}")
        out.append("")
        for r in flagged[:5]:
            out.append(
                f"- {r['date']} · {r['merchant']} · {fmt_money(r['amount'])} · "
                f"risk {r['risk_score']:.2f}" + (f" — {r['reason']}" if r["reason"] else "")
            )
        out.append("")
    out.append("## Tips")
    out.append("")
    out.append("\n".join(f"- {t}" for t in tips[:3]))
    out.append("")
    out.append("---")
    from finance_agent.tools import blend_description

    out.append(
        "_" + blend_description(facts.cfg.get("risk", {}), rule_only=facts.rule_only()) + "_"
    )
    return "\n".join(out)


def send_slack(webhook_url: str, text: str) -> None:
    """POST `text` to a Slack Incoming Webhook. Raises on transport/HTTP errors."""
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — user-configured webhook
            if resp.status >= 400:
                raise RuntimeError(f"Slack webhook returned HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Slack webhook returned HTTP {exc.code}") from exc


def send_email(cfg: dict[str, Any], subject: str, body: str) -> None:
    """Send a plain-text email over SMTP (TLS). Raises on any failure."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = ", ".join(cfg["to_addrs"])
    msg.set_content(body)
    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=15) as server:
        server.starttls()
        if cfg["smtp_user"]:
            server.login(cfg["smtp_user"], cfg["smtp_password"])
        server.send_message(msg)


def run_digest(config_path: str = "config.yaml", out_path: str | None = None) -> str:
    """Build + persist the weekly digest, then deliver via any configured channel.

    Returns the digest Markdown. Slack/email delivery is skipped (with a note)
    when unconfigured; a configured channel that fails raises so the scheduled
    job surfaces it (CI marks the run failed rather than silently dropping it).
    """
    import yaml

    from finance_agent.tools import FinanceFacts

    if out_path and ".." in Path(out_path).parts:
        raise ValueError(f"out_path must not traverse outside the project: {out_path!r}")
    with open(config_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    facts = FinanceFacts(config_path)
    md = build_weekly_digest(facts)
    path = out_path or str(cfg.get("digest", {}).get("out_path", "reports/weekly_digest.md"))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(md)

    webhook = _slack_webhook(cfg)
    if webhook:
        send_slack(webhook, md[:3900])  # Slack message cap ≈ 4000 chars
        log.info("Posted digest to Slack webhook (%d chars).", len(md))
    email = _email_cfg(cfg)
    if email["to_addrs"] and email["smtp_host"]:
        send_email(email, "FinSight Weekly Digest", md)
        log.info("Emailed digest to %s.", ", ".join(email["to_addrs"]))
    if not webhook and not (email["to_addrs"] and email["smtp_host"]):
        log.info(
            "No delivery channel configured (DIGEST_SLACK_WEBHOOK or digest.email) — wrote file only."
        )
    log.info("Weekly digest written to %s", path)
    return md


if __name__ == "__main__":  # pragma: no cover
    run_digest()
