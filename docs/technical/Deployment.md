# Deployment — FinSight Agent: Environments, CI/CD, Rollback

| Field | Value |
| --- | --- |
| Version | v0.2 |
| Last Updated | 2026-08-07 |
| Owner | DevOps Engineer |
| Status | Approved |

---

## 1. Service Topology

| Service | Purpose | Port |
| --- | --- | --- |
| streamlit app | UI (a client of the API when `FINSIGHT_API_URL` is set) | 8501 |
| facts API (`finance_agent/api.py`) | Versioned FastAPI facts service, OpenAPI at `/docs`; `make api` | 8000 |
| docker compose | both services, shared data + model volumes | 8501 + 8000 |

In compose, the `api` service bootstraps data + model on first start
(`docker-entrypoint.sh`, idempotent — named volumes persist artifacts), and the
`finsight` service waits for `api` to be healthy before starting, so there is no
bootstrap race and no retrain on `docker compose restart`.

## 2. CI/CD Pipeline

```mermaid
graph LR
    P[push / PR] --> L[Lint — ruff (Python 3.10 + 3.12)]
    P --> T[Typecheck — mypy]
    P --> T1[Tests — fast + slow + coverage gate]
    P --> LC[Lockfile — recompile + diff]
    P --> SEC[Security — pip-audit + 3.10 dry-run install]
    P --> IC[Install check — anthropic stays optional]
    P --> D[Docs-code consistency gate]
    N[nightly / dispatch] --> B[benchmark-nightly — data + train + artifact + wall-clock gates]
    N --> R[data-realism — realism suite + 60s generation budget]
    N --> M[mutation — mutmut kill score vs regression floor (D.4)]
    N --> CF[contract-fuzz — schemathesis vs committed OpenAPI (F.3)]
    N --> LT[load-test — Locust 50 users vs SLOs (F.5)]
    S[weekly schedule] --> RET[retrain.yml — regenerate + retrain, opens a PR for review]
    S --> DIG[digest.yml — weekly digest delivery]
    S --> A11Y[accessibility — axe-core + mobile render (F.4)]
```

Every push/PR runs the seven CI jobs above (`.github/workflows/ci.yml`); the
nightly benchmark, data-realism, mutation, contract-fuzz, and load-test jobs,
plus the weekly retrain, digest, and accessibility workflows, are
schedule/dispatch-only and never run on every push.

The facts API exposes a module-level `app` (`finance_agent/api.py`), so
`uvicorn finance_agent.api:app` — the launch line used by `make api`,
docker-compose, `fly.toml`, and `docker-entrypoint.sh` — boots directly.

## 3. Environment Promotion

| Step | From | To | Trigger |
| --- | --- | --- | --- |
| 1 | main | main artifacts | CI green |
| 2 | weekly | refreshed artifacts | retrain.yml schedule |

## 4. Rollback Procedure

- Revert artifact commit (model metadata + charts).
- Re-run `make train` on pinned commit.

## 5. Feature Flags & Environment Variables

| Variable | Purpose | Default | Where it's used |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | Enables the real Claude tool-use agent (session-only in the app) | unset → offline narrator | `app/pages/6_Settings.py`, `finance_agent/agent.py` |
| `APP_PASSWORD` | Demo-grade shared-password gate for the app | unset → "DEMO MODE — NOT SECURED" banner | `app/common.py::require_auth()` |
| `FINSIGHT_API_URL` | App becomes an HTTP client of the facts API | unset → local facts | `app/common.py::get_facts()` |
| `FINSIGHT_API_KEY` | Optional shared secret for the API (`X-API-Key` header) | unset → no gate | `finance_agent/api.py` |
| `FINSIGHT_RATE_LIMIT_PER_MIN` | Per-IP sliding-window limit for `/api/*` (in-process) | 0 (off) | `finance_agent/api.py` |
| `FINSIGHT_CORS_ORIGINS` | CORS allow-list for the API (comma-separated) | `*` (local dev only) | `finance_agent/api.py` |
| `data.store_path` (config.yaml) | Optional SQLite persistence path | `data/transactions.db` | `finance_agent/storage.py` |
| `risk.blend` (config.yaml) | Blended risk-score weights | rules/supervised/iforest `0.4/0.3/0.3` | `finance_agent/tools.py` |
| `agent.*` (config.yaml) | Model string, loop turns, session budgets | see config.yaml | `finance_agent/agent.py` |

`docker compose up` passes `APP_PASSWORD`, `FINSIGHT_API_KEY`, and `ANTHROPIC_API_KEY` through from
your shell environment (see `docker-compose.yml`).

## 6. On-Call / Runbook

- **CI red:** check lint/type/tests; fix and re-push.
- **Retrain produced worse model:** review benchmark diff; revert if PR-AUC moved badly.
- **App won't boot:** `make run` deps missing → re-setup.

## 7. Related Documents

| Document | Relationship |
| --- | --- |
| [TechSpec.md](TechSpec.md) | Environments |
| [SecurityAndCompliance.md](SecurityAndCompliance.md) | Secrets |
| [PRD.md](../product/PRD.md) | Release criteria |
| [AppFlow.md](../design/AppFlow.md) | Flows |
| [Schema.md](Schema.md) | Artifacts |
| [Design.md](../design/Design.md) | Design |
| [ImplementationPlan.md](../project/ImplementationPlan.md) | CI tasks |
| [Tracker.md](../project/Tracker.md) | Status |
| [Rules.md](../project/Rules.md) | Standards |
| [API.md](API.md) | Tool contracts |
| [Testing.md](Testing.md) | CI gates |
| [Glossary.md](../reference/Glossary.md) | Vocabulary |
| [RiskRegister.md](../project/RiskRegister.md) | Risks |
