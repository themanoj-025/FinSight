# API — FinSight Agent: HTTP API + Agent Tool Contracts

| Field | Value |
| --- | --- |
| Version | v1 (routes under `/api/v1`) |
| Last Updated | 2026-08-07 |
| Owner | Backend Engineer |
| Status | Approved |

---

> Since v0.2 the facts layer is also exposed as a **versioned HTTP API**
> (`finance_agent/api.py`), and the Streamlit app becomes a client of it when
> `FINSIGHT_API_URL` is set. This documents the HTTP contract; the agent/CLI
> still call the underlying tools directly (section 6).

## 1. Service Overview

| | |
| --- | --- |
| Base URL | `http://localhost:8000` (`make api`) |
| Version prefix | `/api/v1` |
| Interactive docs | `GET /docs` (Swagger UI) · `GET /openapi.json` |
| Format | JSON only; numpy/pandas scalars are converted, so responses are strict JSON (no `NaN`/`Infinity`) |
| Auth | Optional shared secret — if `FINSIGHT_API_KEY` is set, every `/api/*` request must send `X-API-Key: <key>` (demo-grade, see KNOWN_LIMITATIONS) |
| Caching | The API serves a startup snapshot; `POST /api/v1/reload` (or a restart) rebuilds it after data/model artifacts change |

## 2. Endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/` | Service info (name, version, links to docs/health) |
| GET | `/api/v1/health` | `{status, version, rows, rule_only, focal_user, focal_users}` — 503 until data exists |
| GET | `/api/v1/meta` | Config snapshot + `rule_only` / `scoring_mode` / `fraud_threshold` + `focal_user` / `focal_users` |
| GET | `/api/v1/transactions` | Paginated ledger: `?focal_only=true&user=U_X&limit=500&offset=0` (limit ≤ 5000). Returns `{columns, dtypes, data, limit, offset, total, truncated}` so clients page instead of pulling the whole ledger (C.1.3) |
| GET | `/api/v1/monthly-summary` | `?month=YYYY-MM` (default: latest) · `?user=U_X` |
| GET | `/api/v1/category-breakdown` | `?month=YYYY-MM` · `?user=U_X` |
| GET | `/api/v1/budget-status` | Per-category monthly budget tracker — `?month=YYYY-MM` · `?user=U_X` |
| GET | `/api/v1/recurring-payments` | Recurring payments with stable amounts/intervals · `?user=U_X` |
| GET | `/api/v1/spend-spikes` | Detected spending spikes |
| GET | `/api/v1/financial-health` | Health score + components · `?user=U_X` |
| GET | `/api/v1/forecast` | Next-month projection + history · `?user=U_X` |
| GET | `/api/v1/risk-scored` | `?limit&threshold&focal_only&include_explanations&user` — risk-ranked flagged rows (+ per-row SHAP `explanation` when requested and a bundle is present) |
| GET | `/api/v1/tips` | Top 3 data-backed suggestions · `?user=U_X` |
| GET | `/api/v1/similar-transactions` | Phase B.1 — k nearest transactions in feature space with fraud labels: `?transaction_id=<row position>&k=5&user=U_X` (omit `transaction_id` to explain the top-risk flagged transaction) |
| GET | `/metrics` | Prometheus text-format metrics (request counts/latency by route, uptime, version) — see [SLOs.md](SLOs.md) |
| POST | `/api/v1/reload` | Drop the cached facts snapshot (called by the app after data regeneration) |

Every facts endpoint returns the same shape the tools produce:
`{"summary": "<human-readable prose>", "data": {…}}` — the client passes these
through unchanged, so page code is identical in local and API mode.

**Multi-user:** endpoints marked `?user=U_X` accept an optional focal-user id
(from `meta.focal_users`); omitted, they default to the config's `data.focal_user`.
Each user gets its own cached `FinanceFacts` instance, so per-user numbers are
scoped and consistent.

### Example — risk-scored

```http
GET /api/v1/risk-scored?limit=5&threshold=0.7&focal_only=true&include_explanations=true
```

```json
{
  "summary": "3 of 1776 transactions (0.2%) score above 0.70; showing the top 3.",
  "data": {
    "threshold": 0.7,
    "rows": [
      {
        "date": "2025-02-27",
        "merchant": "CryptoExchange",
        "amount": 1945.79,
        "risk_score": 0.996,
        "reason": "balance drain; model fraud probability 0.99",
        "explanation": {
          "method": "TreeSHAP (LightGBM pred_contrib)",
          "bias": -6.2,
          "base_probability": 0.002,
          "top_features": [{"feature": "amount_vs_typical", "contribution": 5.1}]
        }
      }
    ],
    "total_scored": 1776,
    "flagged_count": 3,
    "scoring_mode": "blended",
    "explanations_available": true
  }
}
```

## 3. Errors

| Code | Meaning |
| --- | --- |
| 401 | Missing/invalid `X-API-Key` when `FINSIGHT_API_KEY` is configured |
| 404 | Unknown route |
| 422 | Invalid query params (FastAPI validation) |
| 503 | Data artifacts missing (run `make data` / `make train` first) |

Errors return `{"detail": "<message>"}`. The app client (`app/api_client.py`)
raises `ApiClientError` with status + detail; `get_facts()` falls back to local
facts with a visible warning if the API is unreachable.

## 4. The app as a client

- Set `FINSIGHT_API_URL` (e.g. `http://api:8000` in docker compose) and
  optionally `FINSIGHT_API_KEY`.
- `app/common.py::get_facts()` then returns an `ApiClient` instead of
  `FinanceFacts`; every page keeps working unchanged because the client mirrors
  the facts interface (`df`, `cfg`, `rule_only()`, `focal_users`, the ten tool
  methods).
- After data regeneration the app calls `POST /api/v1/reload` via
  `clear_all_caches()` so the server drops its stale snapshot.

## 5. OpenAPI

The schema is generated by FastAPI from the route signatures — no hand-written
spec to drift. Serve and browse:

```bash
make api        # http://localhost:8000/docs
```

## 6. Underlying Tool Contracts (facts layer)

The HTTP endpoints are thin wrappers over these tools (the agent and CLI call
them directly):

| Tool | Purpose | Input → Output |
| --- | --- | --- |
| monthly_summary | Aggregate by month | month → summary |
| category_breakdown | Spend by category | period → table |
| budget_status | Per-category budget tracker | period → goals vs spend + over flags |
| recurring_payments | Detect recurring | — → list |
| spend_spikes | Detect spend spikes | — → list |
| financial_health | Financial health score | — → score + reasons |
| forecast_next_month | Simple projection | — → forecast |
| risk_scored_transactions | Score transactions | limit/threshold/focal_only/include_explanations → scores |
| top_tips | Data-backed suggestions | — → 3 tips (goals echoed from `config.yaml advice.*`) |
| find_similar_transactions | "Why is this flagged — what does it look like?" | transaction_id/k → k nearest neighbours with fraud-archetype labels (gated by `features.faiss_retrieval`) |

These match `TOOL_SPECS` in `finance_agent/agent.py`; the tool result payload is
the JSON-serialized `data` field (see `tool_result_payload`).

## 7. CLI Surface

| Command | Description |
| --- | --- |
| `finsight ask "..."` / `python -m finance_agent ask "..."` | One-shot agent question |
| `finsight chat` / `python -m finance_agent chat` | Interactive chat |
| `finsight report` / `python -m finance_agent report` | Generate Markdown report |
| `finsight digest` / `python -m finance_agent digest` | Build + deliver the weekly digest (Slack/email via `digest` config; file-only when unconfigured) |

`finsight` is the installed console script (pyproject `[project.scripts]`); the `python -m` form
works without installation.

## 8. Failure & Degradation Behavior

| Condition | What actually happens |
| --- | --- |
| Tool raises | `_execute_tool` returns `{"error": <message>}`; the LLM loop gets it as a `tool_result` and the activity log records `ok: false` |
| Unknown tool name | `{"error": "Unknown tool <name>"}` (never crashes the loop) |
| LLM call fails mid-loop | Falls back to the offline narrator with a visible prefix |
| No API key / invalid key | `llm_available()` is false; the narrator is used (Settings shows "invalid key", never a false "connected ✓") |
| Model bundle missing | Rule-only mode: blend renormalizes to the rule score (weight 1.0); UI shows a "rule-only" badge |
| Model bundle tampered / signature mismatch | Refused before `joblib.load` (HMAC-SHA256, C.2.4); logs an error and degrades to rule-only — never deserializes untrusted bytes |
| No data file | `app/common.py::ensure_data()` generates it with a spinner |
| API unreachable | `get_facts()` warns and falls back to local facts (offline mode) |

## 9. Versioning Policy

- HTTP: routes are versioned (`/api/v1`); breaking changes move to `/api/v2`.
- Tool signatures versioned in code; config.yaml governs behavior.

## 10. Related Documents

| Document | Relationship |
| --- | --- |
| [TechSpec.md](TechSpec.md) | Tool + API layer |
| [Schema.md](Schema.md) | Output data |
| [SecurityAndCompliance.md](SecurityAndCompliance.md) | Key handling |
| [AppFlow.md](../design/AppFlow.md) | Chat flow |
| [PRD.md](../product/PRD.md) | Requirements |
| [Design.md](../design/Design.md) | Chat UI |
| [ImplementationPlan.md](../project/ImplementationPlan.md) | Tasks |
| [Tracker.md](../project/Tracker.md) | Status |
| [Rules.md](../project/Rules.md) | Standards |
| [Testing.md](Testing.md) | API tests |
| [Deployment.md](Deployment.md) | Deploy / compose topology |
| [Glossary.md](../reference/Glossary.md) | Vocabulary |
| [RiskRegister.md](../project/RiskRegister.md) | Risks |
| [KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md) | Honest scope |
