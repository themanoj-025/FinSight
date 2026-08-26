"""Fact tools — deterministic data retrieval and financial analysis.

Every tool returns ``{"summary": str, "data": <jsonable object>}``. The summary is
human-readable prose and the data is structured, so the LLM layer only ever
writes narrative from these outputs and never invents numbers.

This module has no LLM dependency: it is fully offline and unit-testable.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from finance_agent import rules
from finance_agent._facts_base import _FinanceFactsBase, fmt_money


class FactTools(_FinanceFactsBase):
    """Facts provider: monthly summaries, budgets, health, forecasts.

    Inherits shared state and utilities from ``_FinanceFactsBase``.
    """

    def monthly_summary(
        self, month: str | None = None, account_type: str | None = None
    ) -> dict[str, Any]:
        d = self._for_month(month, account_type=account_type)
        key = self._month_key(month)
        if d.empty:
            return {"summary": f"No transactions recorded for {key}.", "data": {}}
        income, savings_out, expenses = self._income_savings_expenses(d)
        net = income - expenses - savings_out
        savings_rate = net / income if income > 0 else 0.0
        top = (
            d.loc[rules.expense_rows(d)]
            .groupby("category")["amount"]
            .sum()
            .sort_values(ascending=False)
        )
        top_cat = str(top.index[0]) if not top.empty else "none"
        summary = (
            f"{key}: income {fmt_money(income)}, expenses {fmt_money(expenses)}, "
            f"net {fmt_money(net)}, savings rate {savings_rate * 100:.1f}%. "
            f"Largest expense category: {top_cat}."
        )
        return {
            "summary": summary,
            "data": {
                "month": key,
                "income": round(income, 2),
                "expenses": round(expenses, 2),
                "net": round(net, 2),
                "savings_rate": round(savings_rate, 4),
                "top_category": top_cat,
                "transaction_count": int(len(d)),
            },
        }

    def category_breakdown(
        self, month: str | None = None, account_type: str | None = None
    ) -> dict[str, Any]:
        d = self._for_month(month, account_type=account_type)
        key = self._month_key(month)
        spend = d.loc[rules.expense_rows(d)].groupby("category")["amount"].sum()
        spend = spend[spend.index != "savings"] if "savings" in spend.index else spend
        spend = spend.sort_values(ascending=False)
        total = float(spend.sum())
        rows: list[dict[str, Any]] = [
            {
                "category": str(c),
                "amount": round(float(a), 2),
                "share": round(float(a) / total, 3) if total else 0.0,
            }
            for c, a in spend.items()
        ]
        summary = (
            f"{key} spending by category: "
            + ", ".join(f"{r['category']} {fmt_money(r['amount'])}" for r in rows[:4])
            + (f" (total {fmt_money(total)})." if total else " (no spending).")
        )
        return {"summary": summary, "data": {"month": key, "total": round(total, 2), "rows": rows}}

    def budget_status(
        self, month: str | None = None, account_type: str | None = None
    ) -> dict[str, Any]:
        """Per-category monthly spend vs. configured budget goals.

        Goals come from `config.yaml budgets.monthly` (category -> amount). A
        category with no goal is not tracked. Returns spend, goal, and the
        fraction of the goal used, so the UI can render progress bars.
        """
        d = self._for_month(month, account_type=account_type)
        key = self._month_key(month)
        goals = (self.cfg.get("budgets") or {}).get("monthly") or {}
        if not goals:
            return {
                "summary": "No budget goals configured (add `budgets.monthly` to config.yaml).",
                "data": {"month": key, "configured": False, "rows": []},
            }
        spend = d.loc[rules.expense_rows(d)].groupby("category")["amount"].sum()
        spend = spend[spend.index != "savings"] if "savings" in spend.index else spend
        rows: list[dict[str, Any]] = []
        for cat, goal in goals.items():
            goal_f = float(goal)
            spent_f = round(float(spend.get(cat, 0.0)), 2)
            pct = spent_f / goal_f if goal_f > 0 else 0.0
            rows.append(
                {
                    "category": str(cat),
                    "goal": round(goal_f, 2),
                    "spent": spent_f,
                    "pct": round(pct, 3),
                    "over": pct > 1.0,
                }
            )
        rows.sort(key=lambda r: r["pct"], reverse=True)
        over = [r["category"] for r in rows if r["over"]]
        total_goal = round(sum(r["goal"] for r in rows), 2)
        total_spent = round(sum(r["spent"] for r in rows), 2)
        if over:
            summary = (
                f"{key} budget tracker: {len(over)} of {len(rows)} categories over "
                f"their goal ({', '.join(over)}); overall {total_spent:.0f} of "
                f"{total_goal:.0f} ({total_spent / total_goal * 100:.0f}%) used."
            )
        else:
            summary = (
                f"{key} budget tracker: all {len(rows)} tracked categories within "
                f"goal; overall {total_spent:.0f} of {total_goal:.0f} "
                f"({total_spent / total_goal * 100:.0f}%) used."
            )
        return {
            "summary": summary,
            "data": {
                "month": key,
                "configured": True,
                "rows": rows,
                "total_goal": total_goal,
                "total_spent": total_spent,
            },
        }

    def recurring_payments(self) -> dict[str, Any]:
        rec = rules.detect_recurring_payments(self._focal())
        if rec.empty:
            return {
                "summary": "No recurring payments detected with stable amounts and intervals.",
                "data": {"rows": []},
            }
        monthly = float(rec["amount"].sum())
        preview = list(zip(rec["merchant"], rec["amount"], rec["interval_days"], strict=True))[:5]
        summary = (
            f"{len(rec)} recurring payments totaling ~{fmt_money(monthly)}/month: "
            + ", ".join(f"{m} {fmt_money(a)} every {i:.0f} days" for m, a, i in preview)
        )
        return {
            "summary": summary,
            "data": {"rows": rec.to_dict(orient="records"), "monthly_total": round(monthly, 2)},
        }

    def spend_spikes(self) -> dict[str, Any]:
        spikes = rules.detect_spend_spikes(self.df)
        if spikes.empty:
            return {"summary": "No category spending spikes detected.", "data": {"rows": []}}
        rows = spikes[["step", "date", "category", "merchant", "amount"]].to_dict(orient="records")
        total = float(spikes["amount"].sum())
        cats = ", ".join(sorted(set(spikes["category"])))
        summary = (
            f"{len(spikes)} transactions in a spending spike ({cats}, {fmt_money(total)} total)."
        )
        return {"summary": summary, "data": {"rows": rows, "total": round(total, 2)}}

    def financial_health(self) -> dict[str, Any]:
        health = rules.compute_financial_health(self._focal())
        summary = (
            f"Financial health score {health['score']}/100 — savings rate "
            f"{health['savings_rate'] * 100:.1f}%, ~{health['buffer_months']:.1f} "
            f"months of expenses in buffer."
        )
        return {"summary": summary, "data": health}

    def forecast_next_month(self) -> dict[str, Any]:
        d = self._focal()
        d["_month"] = pd.to_datetime(d["datetime"]).dt.strftime("%Y-%m")
        d["_amount"] = d["amount"]
        monthly = (
            d.groupby("_month")
            .apply(
                lambda g: pd.Series(
                    {
                        "income": float(g.loc[g["type"].isin(rules.CREDIT_TYPES if hasattr(rules, "CREDIT_TYPES") else ["CREDIT", "SALARY"]), "amount"].sum()),
                        "expenses": float(g.loc[rules.expense_rows(g), "amount"].sum()),
                    }
                ),
                include_groups=False,
            )
            .sort_index()
        )
        if monthly.empty:
            return {"summary": "Not enough history to forecast.", "data": {}}
        months = list(monthly.index)
        incomes = monthly["income"].to_numpy(dtype=float)
        expenses = monthly["expenses"].to_numpy(dtype=float)
        n = len(months)
        slope = lambda v: float(np.polyfit(np.arange(n), v, 1)[0]) if n >= 2 else 0.0  # noqa: E731
        f_income = max(0.0, float(incomes[-1]) + slope(incomes))
        f_expenses = max(0.0, float(expenses[-1]) + slope(expenses))
        trend = (
            "rising" if slope(expenses) > 0 else ("falling" if slope(expenses) < 0 else "stable")
        )
        summary = (
            f"Next month projection: income ~{fmt_money(f_income)}, expenses "
            f"~{fmt_money(f_expenses)}, net ~{fmt_money(f_income - f_expenses)} "
            f"(expense trend {trend})."
        )
        return {
            "summary": summary,
            "data": {
                "forecast_income": round(f_income, 2),
                "forecast_expenses": round(f_expenses, 2),
                "forecast_net": round(f_income - f_expenses, 2),
                "trend": trend,
                "history": [
                    {"month": m, "income": round(float(i), 2), "expenses": round(float(e), 2)}
                    for m, i, e in zip(months, incomes, expenses, strict=True)
                ],
            },
        }
