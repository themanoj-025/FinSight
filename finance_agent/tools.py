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
from typing import Any

from finance_agent.facts_tools import FactTools
from finance_agent.retrieval_tools import RetrievalTools
from finance_agent.risk_tools import RiskTools

# Re-export public helpers so existing imports keep working:
#   from finance_agent.tools import load_config, blend_description, ...
from finance_agent._facts_base import (  # noqa: F401
    DEFAULT_BLEND,
    _blend_weights,
    _FinanceFactsBase,
    blend_description,
    income_mask,
    income_savings_expenses,
    load_config,
    monthly_income_expenses,
    savings_out_mask,
)


class FinanceFacts(FactTools, RiskTools, RetrievalTools):
    """Combined facts provider: facts + risk + retrieval via multiple inheritance.

    MRO: FinanceFacts → FactTools → RiskTools → RetrievalTools → _FinanceFactsBase

    The ``__init__`` lives in ``_FinanceFactsBase`` and is called once; all
    three tool categories share the same config, ledger, bundle, and store.
    """

    pass


def tool_result_payload(result: dict[str, Any]) -> str:
    """Compact JSON for an LLM tool_result — numbers only ever come from here."""
    return json.dumps(result["data"], default=str)
