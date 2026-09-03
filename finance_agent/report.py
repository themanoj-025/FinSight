"""Monthly report generation — a self-contained Markdown digest of the account.

Every figure comes from the facts layer (`tools.py`); the report is just a
templated view over tool outputs.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Protocol

from finance_agent.constants import fmt_money
from finance_agent.tools import FinanceFacts


class FactsSource(Protocol):
    """Duck-typed facts provider — `FinanceFacts` or the app's `ApiClient`.

    Keeps the report honest: it only needs this surface, and both the local
    facts layer and the HTTP client (app/api_client.py) satisfy it.
    """

    cfg: dict[str, Any]
    df: Any

    def rule_only(self) -> bool: ...
    def _focal(self) -> Any: ...
    def monthly_summary(self, month: str | None = None) -> dict[str, Any]: ...
    def category_breakdown(self, month: str | None = None) -> dict[str, Any]: ...
    def budget_status(self, month: str | None = None) -> dict[str, Any]: ...
    def recurring_payments(self) -> dict[str, Any]: ...
    def spend_spikes(self) -> dict[str, Any]: ...
    def financial_health(self) -> dict[str, Any]: ...
    def forecast_next_month(self) -> dict[str, Any]: ...
    def risk_scored_transactions(
        self,
        limit: int = 15,
        threshold: float | None = None,
        focal_only: bool = False,
        include_explanations: bool = False,
    ) -> dict[str, Any]: ...
    def top_tips(self) -> dict[str, Any]: ...


def _model_note() -> str:
    meta_path = "model_bench/best_model_metadata.json"
    if not os.path.exists(meta_path):
        return "No trained model on disk — run `make train` to benchmark and select one."
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    m = meta.get("metrics_on_holdout", {})
    pr_mean = meta.get("pr_auc_mean")
    pr_std = meta.get("pr_auc_std")
    cv = (
        f" — CV PR-AUC {pr_mean:.3f} ± {pr_std:.3f} ({meta.get('cv_folds', '?')}-fold "
        "time-series split)"
        if pr_mean is not None
        else ""
    )
    return (
        f"Best model: **{meta.get('algorithm')}** (selected by "
        f"{meta.get('selection_metric')}) — holdout PR-AUC {m.get('pr_auc', 'n/a')}, "
        f"ROC-AUC {m.get('roc_auc', 'n/a')}, F1 {m.get('f1', 'n/a')}{cv}. "
        "Evaluation uses a temporal split with strictly backward-looking features "
        "(see docs/KNOWN_LIMITATIONS.md)."
    )


def build_report(facts: FactsSource | None = None) -> str:
    # A fresh annotated local (mypy won't narrow a reassigned parameter past
    # the Protocol union). `FinanceFacts` and the app's `ApiClient` both
    # satisfy `FactsSource` structurally.
    source: FactsSource = FinanceFacts() if facts is None else facts
    summary = source.monthly_summary()
    breakdown = source.category_breakdown()
    budgets = source.budget_status()
    recurring = source.recurring_payments()
    spikes = source.spend_spikes()
    health = source.financial_health()
    forecast = source.forecast_next_month()
    risk = source.risk_scored_transactions(limit=12)
    tips = source.top_tips()

    out: list[str] = []
    out.append("# FinSight Agent — Monthly Report")
    out.append("")
    out.append(
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · fully offline_"
    )
    out.append("")
    out.append("## Executive summary")
    out.append("")
    out.append(summary["summary"])
    out.append("")
    out.append(f"- Financial health score: **{health['data']['score']}/100**")
    out.append(f"- {forecast['summary']}")
    out.append("")
    out.append("## Spending by category")
    out.append("")
    out.append("| Category | Amount | Share |")
    out.append("|---|---|---|")
    for row in breakdown["data"]["rows"]:
        out.append(
            f"| {row['category']} | {fmt_money(row['amount'])} | {row['share'] * 100:.1f}% |"
        )
    out.append("")
    out.append("## Budget tracker")
    out.append("")
    if budgets["data"].get("configured") and budgets["data"].get("rows"):
        out.append("| Category | Spent | Goal | Used |")
        out.append("|---|---|---|---|")
        for row in budgets["data"]["rows"]:
            flag = " ⚠️ over" if row["over"] else ""
            out.append(
                f"| {row['category']} | {fmt_money(row['spent'])} | "
                f"{fmt_money(row['goal'])} | {row['pct'] * 100:.0f}%{flag} |"
            )
    else:
        out.append("No budget goals configured — add `budgets.monthly` to config.yaml.")
    out.append("")
    out.append("## Recurring payments")
    out.append("")
    if recurring["data"].get("rows"):
        out.append("| Merchant | Category | Amount | Interval (days) | Occurrences | Last paid |")
        out.append("|---|---|---|---|---|---|")
        for row in recurring["data"]["rows"]:
            out.append(
                f"| {row['merchant']} | {row['category']} | {fmt_money(row['amount'])} "
                f"| {row['interval_days']} | {row['occurrences']} | {row['last_paid']} |"
            )
    else:
        out.append("No stable recurring payments detected.")
    out.append("")
    out.append("## Fraud & anomaly scan")
    out.append("")
    out.append(risk["summary"])
    out.append("")
    rows = risk["data"]["rows"]
    if rows:
        out.append("| Date | Merchant | Category | Amount | Risk | Signal |")
        out.append("|---|---|---|---|---|---|")
        for row in rows[:12]:
            out.append(
                f"| {row['date']} | {row['merchant']} | {row['category']} | "
                f"{fmt_money(row['amount'])} | {row['risk_score']:.2f} | {row['reason'] or '—'} |"
            )
    else:
        out.append("No anomalies flagged this period — your account looks clean.")
    out.append("")
    if spikes["data"].get("rows"):
        out.append(f"Spending spikes: {spikes['summary']}")
        out.append("")
    out.append("## Health & recommendations")
    out.append("")
    out.append(
        f"- **Health score {health['data']['score']}/100** — savings rate "
        f"{health['data']['savings_rate'] * 100:.1f}%, buffer "
        f"~{health['data']['buffer_months']} months."
    )
    out.append("- " + "\n- ".join(tips["data"]["tips"]))
    out.append("")
    out.append("## Model card")
    out.append("")
    out.append(_model_note())
    out.append("")
    out.append("---")
    from finance_agent.tools import blend_description

    out.append(
        "_" + blend_description(source.cfg.get("risk", {}), rule_only=source.rule_only()) + "_"
    )
    out.append("")
    return "\n".join(out)


def write_report(path: str | None = None, facts: FactsSource | None = None) -> str:
    path = path or "reports/monthly_report.md"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_report(facts))
    return path


if __name__ == "__main__":  # pragma: no cover
    print(write_report())
