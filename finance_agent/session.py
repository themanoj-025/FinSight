"""Per-session LLM observability and budget guardrails.

Extracted from ``agent.py`` to keep the reasoning layer focused on the
tool-use loop.  ``SessionUsage`` tracks real API usage; ``SessionBudget``
enforces turn/token caps and survives page reloads via SQLite persistence.
"""

from __future__ import annotations

from typing import Any


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for budget accounting."""
    return max(0, len(text) // 4)


# Fallback list prices (USD per 1M tokens) for models without an entry in
# config.yaml `agent.pricing`.  Sonnet-class numbers; the config is the source
# of truth — this is only a safety net so cost accounting never crashes.
DEFAULT_PRICING: dict[str, float] = {"input_per_1m": 3.0, "output_per_1m": 15.0}


def estimate_cost(
    agent_cfg: dict[str, Any], model: str, input_tokens: int, output_tokens: int
) -> float -> None:
    """Estimated USD cost of one call, from config ``agent.pricing``.

    Prices are per 1M tokens; unknown models fall back to ``DEFAULT_PRICING``.
    This is an *estimate* (the API's exact billing may differ) — the Settings
    dashboard labels it as such.
    """
    pricing = (agent_cfg.get("pricing") or {}).get(model) or DEFAULT_PRICING
    inp = float(pricing.get("input_per_1m", DEFAULT_PRICING["input_per_1m"]))
    out = float(pricing.get("output_per_1m", DEFAULT_PRICING["output_per_1m"]))
    return input_tokens / 1_000_000 * inp + output_tokens / 1_000_000 * out


class SessionUsage:
    """Per-session LLM observability — real usage, not just budget guards.

    One record per API call (or offline-narrator turn), with the token counts
    the API actually reported, wall-clock latency, and an estimated cost from
    ``agent.pricing``.  Lives on the (session-cached) ``FinanceAgent``; the
    Settings page renders it.  Narrator turns are recorded too, at zero cost,
    so the dashboard is an honest ledger of how the session spent its budget.
    """

    def __init__(self, pricing: dict[str, Any] | None = None) -> None:
        self.pricing = pricing or {}
        self.calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []

    def record_llm(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        cache_read: int = 0,
        cache_write: int = 0,
        ok: bool = True,
        error: str = "",
    ) -> None -> None:
        self.calls.append(
            {
                "kind": "llm",
                "model": model,
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "cache_read_tokens": int(cache_read),
                "cache_write_tokens": int(cache_write),
                "latency_ms": round(latency_ms, 1),
                "est_cost": round(
                    estimate_cost(self.pricing, model, input_tokens, output_tokens), 6
                ),
                "ok": ok,
                "error": error,
            }
        )

    def record_narrator(self, latency_ms: float = 0.0) -> None:
        self.calls.append(
            {
                "kind": "narrator",
                "model": "offline narrator",
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "latency_ms": round(latency_ms, 1),
                "est_cost": 0.0,
                "ok": True,
                "error": "",
            }
        )

    def record_tool(self, tool: str, latency_ms: float, ok: bool = True) -> None:
        self.tool_calls.append({"tool": tool, "latency_ms": round(latency_ms, 1), "ok": ok})

    def totals(self) -> dict[str, Any]:
        n = len(self.calls)
        llm = [c for c in self.calls if c["kind"] == "llm"]
        total_in = sum(c["input_tokens"] for c in llm)
        total_out = sum(c["output_tokens"] for c in llm)
        total_cost = round(sum(c["est_cost"] for c in llm), 4)
        latencies = [c["latency_ms"] for c in self.calls if c["latency_ms"] > 0]
        return {
            "calls": n,
            "llm_calls": len(llm),
            "narrator_calls": n - len(llm),
            "failed_calls": sum(1 for c in llm if not c["ok"]),
            "input_tokens": total_in,
            "output_tokens": total_out,
            "cache_read_tokens": sum(c["cache_read_tokens"] for c in llm),
            "cache_write_tokens": sum(c["cache_write_tokens"] for c in llm),
            "est_cost": total_cost,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
            "tool_calls": len(self.tool_calls),
        }

    def recent(self, n: int = 20) -> list[dict[str, Any]]:
        return self.calls[-n:]

    def reset(self) -> None:
        self.calls.clear()
        self.tool_calls.clear()

    def to_dict(self) -> dict[str, Any]:
        return {"totals": self.totals(), "calls": self.calls[-20:]}


class SessionBudget:
    """Per-session LLM cost guardrail — persisted so a reload can't reset it.

    With a ``SessionBudgetStore`` (config ``agent.budget_store``, default
    ``data/session_usage.db``) usage is keyed by session id in SQLite: turn
    counts and the *exact* input/output token counts the Anthropic API reports
    (recorded after each call via ``record_real``) survive page reloads and
    server restarts, so the cap is real server-side enforcement (C.1.2) rather
    than client-side theater.  Without a store (CLI/tests) it degrades to the
    original in-memory accounting.

    When exhausted, the app falls back to the offline narrator instead of
    silently continuing to call the API.
    """

    def __init__(
        self,
        max_turns: int,
        max_tokens: int,
        store: Any | None = None,
        session_id: str = "",
    ) -> None:
        self.max_turns = int(max_turns)
        self.max_tokens = int(max_tokens)
        self.store = store
        self.session_id = str(session_id or "default")
        self._turns_used = 0  # in-memory fallback (no store)
        self._tokens_used = 0
        self._refused: str | None = None

    def _persisted(self) -> dict[str, float] | None:
        if self.store is None:
            return None
        return self.store.totals(self.session_id)

    def turns_used(self) -> int:
        p = self._persisted()
        if p is not None:
            return int(p["turns"])
        return self._turns_used

    def tokens_used(self) -> int:
        p = self._persisted()
        if p is not None:
            return int(p["input_tokens"]) + int(p["output_tokens"])
        return self._tokens_used

    def remaining_turns(self) -> int:
        return max(0, self.max_turns - self.turns_used())

    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.tokens_used())

    def allow(self, approx_tokens: int = 0) -> bool:
        if self.turns_used() >= self.max_turns:
            self._refused = "turn budget"
            return False
        if self.tokens_used() + approx_tokens > self.max_tokens:
            self._refused = "token budget"
            return False
        return True

    def record(self, approx_tokens: int = 0) -> None:
        self._turns_used += 1
        self._tokens_used += approx_tokens
        if self.store is not None:
            self.store.record_turn(self.session_id)

    def record_real(self, input_tokens: int, output_tokens: int, est_cost: float = 0.0) -> None:
        """Record the API's exact token counts (replaces the pre-call estimate)."""
        if self.store is not None:
            self.store.record_usage(self.session_id, input_tokens, output_tokens, est_cost)

    def exhausted_reason(self) -> str | None:
        if self.turns_used() >= self.max_turns:
            return "turn budget"
        if self.tokens_used() >= self.max_tokens:
            return "token budget"
        return self._refused

    def to_dict(self) -> dict[str, int]:
        return {
            "max_turns": self.max_turns,
            "max_tokens": self.max_tokens,
            "turns_used": self.turns_used(),
            "tokens_used": self.tokens_used(),
        }
