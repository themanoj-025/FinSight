"""Facts layer — deterministic Python + the trained model.

Every tool returns ``{"summary": str, "data": <jsonable object>}``. The summary is
human-readable prose and the data is structured, so the LLM layer only ever
writes narrative from these outputs and never invents numbers.

This module has no LLM dependency: it is fully offline and unit-testable.

Thin coordinator: ``FinanceFacts`` inherits from three focused modules:
  - facts_tools.py      (FactTools: monthly, category, budget, health, forecast)
  - risk_tools.py       (RiskTools: scoring, SHAP, tips)
  - retrieval_tools.py  (RetrievalTools: similar transactions, FAISS)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

# Re-export public helpers so existing imports keep working:
#   from finance_agent.tools import load_config, blend_description, ...
from finance_agent._facts_base import (
    DEFAULT_BLEND,
    _blend_weights,
    _FinanceFactsBase,
    _scored_frame_json,
    blend_description,
    income_mask,
    income_savings_expenses,
    load_config,
    monthly_income_expenses,
    savings_out_mask,
)
from finance_agent.facts_tools import FactTools
from finance_agent.retrieval_tools import RetrievalTools
from finance_agent.risk_tools import RiskTools

__all__ = [
    "DEFAULT_BLEND",
    "_FinanceFactsBase",
    "_blend_weights",
    "_scored_frame_json",
    "blend_description",
    "income_mask",
    "income_savings_expenses",
    "load_config",
    "monthly_income_expenses",
    "savings_out_mask",
]


class FinanceFacts(FactTools, RiskTools, RetrievalTools):
    """Combined facts provider: facts + risk + retrieval via multiple inheritance.

    MRO: FinanceFacts → FactTools → RiskTools → RetrievalTools → _FinanceFactsBase

    The ``__init__`` lives in ``_FinanceFactsBase`` and is called once; all
    three tool categories share the same config, ledger, bundle, and store.
    """

    # Cross-mixin surface: every mixin shares one instance at runtime via the
    # common _FinanceFactsBase state, but mypy only sees each mixin's own
    # class. Declare the sibling methods so internal cross-calls type-check.
    if TYPE_CHECKING:  # pragma: no cover

        def forecast_next_month(self) -> dict[str, Any]: ...

        def risk_scored_transactions(
            self,
            limit: int = 15,
            threshold: float | None = None,
            focal_only: bool = False,
            include_explanations: bool = False,
            account_type: str | None = None,
        ) -> dict[str, Any]: ...


def tool_result_payload(result: dict[str, Any]) -> str:
    """Compact JSON for an LLM tool_result — numbers only ever come from here."""
    return json.dumps(result["data"], default=str)
