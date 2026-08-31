"""LLM golden-answer suite (Phase F.1) — "the LLM never invents numbers".

Fixed questions against a fixed dataset snapshot; every numeric figure
(``$`` amounts, percentages) in an answer must match a tool-output value
exactly. Two layers:

1. **Offline narrator** — deterministic end-to-end answers built from tool
   outputs; we assert the figures directly.
2. **LLM tool-use loop** — a scripted Anthropic client that returns a
   ``tool_use`` block and then *echoes the exact tool_result payload* as its
   text; we assert the loop hands the LLM the verbatim payload (no invented
   numbers can enter), and that the echoed figures equal the real tool output.
"""

import json
import re
from types import SimpleNamespace

import pytest

from finance_agent.agent import FinanceAgent

AMOUNT_RE = re.compile(r"(-?\$[\d,]+(?:\.\d+)?)")
PERCENT_RE = re.compile(r"(-?\d+(?:\.\d+)?)%")


def _figures_in_text(text: str) -> list[tuple[float, str]]:
    """(value, kind) pairs — kind is "amount" or "percent"."""
    out = []
    for m in AMOUNT_RE.findall(text):
        out.append((float(m.replace("$", "").replace(",", "")), "amount"))
    for m in PERCENT_RE.findall(text):
        out.append((float(m), "percent"))
    return out


def _floats_in_payload(payload: str) -> set[float]:
    """Every raw number a tool output carries (unrounded)."""
    out: set[float] = set()
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return out

    def walk(node) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            out.add(float(node))

    walk(data)
    return out


def assert_figures_grounded(answer: str, payloads: list[str]) -> None:
    """Every $amount / % figure in `answer` must trace to at least one payload.

    Amounts match payload amounts (2-dp). Percentages are rendered from
    ``share``/``rate`` fractions (fig/100) or derived from counts
    (e.g. 1/259 -> "0.4%").
    """
    figures = _figures_in_text(answer)
    if not figures:
        return  # nothing numeric claimed — trivially grounded
    allowed = set()
    for payload in payloads:
        allowed |= _floats_in_payload(payload)
    assert allowed, "answer carries figures but no tool payload numbers to check against"
    amounts = {round(v, 2) for v in allowed}
    shares = {round(v, 3) for v in allowed}
    nums = sorted(allowed)

    def _pct_ok(fig: float) -> bool:
        """A percentage is grounded if it is a payload share (fig/100) or a
        ratio of two payload numbers within ±1pp (float/`:.0f` rendering
        tolerances, e.g. budget "X% used" or "1 of 259 (0.4%)")."""
        if round(fig / 100, 3) in shares:
            return True
        for a in nums:
            for b in nums:
                if b > 0 and abs(fig - 100.0 * a / b) <= 1.0:
                    return True
        return False

    for fig, kind in figures:
        if kind == "amount":
            assert round(fig, 2) in amounts, (
                f"$figure {fig} in the answer is not in any tool output "
                f"(payload amounts: {sorted(amounts)[:20]}…)"
            )
        else:
            assert _pct_ok(fig), (
                f"% figure {fig} in the answer does not match a payload share "
                f"or a count-derived percentage "
                f"(payload shares: {sorted(shares)[:20]}…)"
            )


@pytest.fixture()
def env(tmp_path) -> dict[str, object]:
    """Fixed dataset snapshot + config (mirrors tests/test_agent.py)."""
    import yaml

    from generate_data import generate

    df = generate(
        days=30,
        seed=7,
        user="U_Alex",
        n_background_accounts=10,
        n_fraud_pairs=2,
        start_date="2025-01-01",
    )
    data_path = tmp_path / "transactions.csv"
    df.to_csv(data_path, index=False)
    cfg = {
        "data": {"path": str(data_path)},
        "model_bench": {"bundle_path": str(tmp_path / "missing.joblib")},
        "risk": {
            "blend": {"rules": 0.4, "supervised": 0.3, "isolation_forest": 0.3},
            "fraud_threshold": 0.7,
        },
        "budgets": {"monthly": {"dining": 350, "groceries": 700, "transport": 150}},
        "agent": {
            "activity_log": str(tmp_path / "activity.jsonl"),
            "max_turns": 3,
            "max_history_turns": 10,
            "max_session_turns": 20,
            "max_session_tokens": 20000,
            "model": "claude-sonnet-4-5",
        },
    }
    cfg_path = tmp_path / "config.yaml"
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)
    return {"cfg_path": str(cfg_path), "tmp": tmp_path}


def _offline(env) -> FinanceAgent:
    return FinanceAgent(env["cfg_path"])  # no key -> narrator


def _tool_payloads(agent: FinanceAgent) -> list[str]:
    """Re-run exactly the tools the narrator called; return their payloads.

    Grounding against the activity log means a multi-tool answer (e.g. the
    summary route = monthly_summary + financial_health + top_tips) is checked
    against every tool output that actually fed it.
    """
    payloads: list[str] = []
    for rec in agent.activity.recent(50):
        result = getattr(agent.facts, rec["tool"])(**rec["args"])
        payloads.append(json.dumps(result["data"], default=str))
    assert payloads, "the narrator should have called at least one tool"
    return payloads


# ------------------------------------------------------------ narrator golden
def test_narrator_category_answer_figures_match_tool_output(env) -> None:
    agent = _offline(env)
    answer = str(agent.answer("How much did I spend on dining?"))
    assert "dining" in answer.lower()
    assert_figures_grounded(answer, _tool_payloads(agent))


def test_narrator_monthly_summary_figures_match_tool_output(env) -> None:
    agent = _offline(env)
    answer = str(agent.answer("What's my monthly summary?"))
    assert_figures_grounded(answer, _tool_payloads(agent))


def test_narrator_risk_figures_match_tool_output(env) -> None:
    agent = _offline(env)
    answer = str(agent.answer("Any suspicious activity?"))
    assert_figures_grounded(answer, _tool_payloads(agent))


def test_narrator_budget_figures_match_tool_output(env) -> None:
    agent = _offline(env)
    answer = str(agent.answer("Am I over budget on dining?"))
    assert_figures_grounded(answer, _tool_payloads(agent))


# ------------------------------------------------------- LLM tool-use golden
class GoldenMessages:
    """Scripted client: tool_use on the first call, then the verbatim
    tool_result payload echoed as the final answer text."""

    def __init__(self, captured, tool_name, tool_input) -> None:
        self._captured = captured
        self._tool_name = tool_name
        self._tool_input = tool_input
        self._n = 0

    @staticmethod
    def _usage() -> None:
        return SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )

    def stream(self, **kwargs) -> None:
        self._captured.append(kwargs)
        if self._n == 0:
            self._n += 1
            block = SimpleNamespace(
                type="tool_use", id="tu_1", name=self._tool_name, input=self._tool_input
            )
            final = SimpleNamespace(stop_reason="tool_use", content=[block], usage=self._usage())
            return _GoldenStream("", final)
        # Second call: the agent has appended a tool_result — echo it verbatim.
        last = self._captured[-1]["messages"][-1]
        payload = last["content"][0]["content"]
        final = SimpleNamespace(stop_reason="end_turn", content=[], usage=self._usage())
        return _GoldenStream(payload, final)


class _GoldenStream:
    def __init__(self, text, final) -> None:
        self.text_stream = iter([text])
        self._final = final

    def __enter__(self) -> None:
        return self

    def __exit__(self, *args) -> bool:
        return False

    def get_final_message(self) -> None:
        return self._final


class GoldenAnthropic:
    def __init__(self, captured, tool_name, tool_input) -> None:
        self.captured = captured
        self._tool_name = tool_name
        self._tool_input = tool_input

    def Anthropic(self, api_key="") -> None:
        return SimpleNamespace(
            messages=GoldenMessages(self.captured, self._tool_name, self._tool_input)
        )


@pytest.mark.parametrize(
    ("question", "tool_name", "tool_input"),
    [
        ("How much did I spend on dining?", "category_breakdown", {}),
        ("Any suspicious activity?", "risk_scored_transactions", {"limit": 10}),
        ("What's my monthly summary?", "monthly_summary", {}),
    ],
)
def test_llm_loop_passes_tool_outputs_verbatim(env, question, tool_name, tool_input) -> None:
    """The tool-use loop must hand the LLM the exact tool_result payload, so
    the only numbers an LLM answer can contain are tool outputs."""
    captured: list[dict] = []
    agent = FinanceAgent(
        env["cfg_path"],
        api_key="sk-test",
        _anthropic=GoldenAnthropic(captured, tool_name, tool_input),
    )
    answer = "".join(agent._llm_answer(question))
    assert answer, "expected an echoed answer"

    # the tool_result the agent sent back to the model is the verbatim payload
    tool_results = [
        msg
        for call in captured
        for msg in call["messages"]
        if msg.get("role") == "user" and isinstance(msg.get("content"), list)
    ]
    assert tool_results, "expected at least one tool_result message"
    payload = tool_results[0]["content"][0]["content"]

    # the answer's figures must equal the tool output's figures
    assert_figures_grounded(answer, [payload])

    # ... and the payload must match what the real tool would have returned
    real = getattr(agent.facts, tool_name)(**tool_input)
    real_payload = json.dumps(real["data"], default=str, sort_keys=True)
    assert json.loads(payload) == json.loads(real_payload)


def test_system_prompt_forbids_inventing_numbers(env) -> None:
    """The guardrail sentence is present in every API call (F.1 companion)."""
    captured: list[dict] = []
    agent = FinanceAgent(
        env["cfg_path"],
        api_key="sk-test",
        _anthropic=GoldenAnthropic(captured, "monthly_summary", {}),
    )
    list(agent._llm_answer("What's my monthly summary?"))
    assert captured
    system = captured[0]["system"]
    assert "Never invent numbers" in system
    assert "from the tool outputs" in system.lower()
