# Architecture

A concise, current map of how FinSight Agent is built. This document is the
canonical architecture reference; the code itself remains the source of truth
(docs-code consistency is enforced by `scripts/check_docs_consistency.py`).

## 1. System at a glance

FinSight Agent is an autonomous personal-finance analyst: a **synthetic data
generator** produces a large, realistic transaction ledger; a **rule + ML
scoring pipeline** flags fraud/anomalies with per-transaction explanations; and
a **Streamlit app** (optionally backed by a **FastAPI facts service**) renders
KPIs, charts, reports, and a chat agent — all reproducible from a seed, with no
real personal data.

## 2. Layered model

```
┌────────────────────────────────────────────────────────────────┐
│  Presentation                                                    │
│   app/ (Streamlit: Dashboard, Transactions, Fraud Detection,     │
│        Ask the Agent, Reports, Settings) · finance_agent.cli     │
│        finance_agent.report / pdf_export / digest                │
├────────────────────────────────────────────────────────────────┤
│  API / Interface                                                 │
│   finance_agent/api.py (FastAPI /api/v1/*) · app/api_client.py   │
├────────────────────────────────────────────────────────────────┤
│  Application / Facts                                             │
│   finance_agent/tools.py (FinanceFacts hub)                      │
│   rules.py (rule detectors) · features.py (model features)       │
│   storage.py (SQLite persistence + migrations)                   │
├────────────────────────────────────────────────────────────────┤
│  Reasoning                                                       │
│   finance_agent/agent.py (LLM tool-use loop + offline narrator)  │
├────────────────────────────────────────────────────────────────┤
│  Domain                                                          │
│   datagen.py (tiered generator) · personas.py · merchants.py     │
│   fraud_patterns.py (15 archetypes) · constants.py               │
├────────────────────────────────────────────────────────────────┤
│  Model benchmark / MLOps                                         │
│   model_bench/ (train_and_compare.py → evaluate.py + models.py)  │
└────────────────────────────────────────────────────────────────┘
```

Dependencies point strictly downward: `app/` → `finance_agent` → `model_bench`,
with `finance_agent.tools` the only bridge to `model_bench.models`. There are no
circular imports (verified by AST scan, see `docs/project/analysis_report.md` §5).

## 3. Runtime flows

### 3.1 Data generation
`generate_data.py --tier tiny|demo|bench` → `finance_agent.datagen.generate_dataset()`
→ vectorized, seed-reproducible transaction stream (personas, accounts,
regions, seasonality, fraud archetypes) → CSV (`demo`) or Parquet (`bench`).
The SQLite store (`storage.py`) is synced from the ledger and risk scores are
materialized once per (data, model) fingerprint.

### 3.2 Model benchmark
`model_bench/train_and_compare.py --data <csv|parquet>` → temporal train/test
split → 6-model TimeSeriesSplit CV (mean ± std PR-AUC) → winner refit →
`best_model.joblib` + `risk_model_bundle.joblib` + `best_model_metadata.json` +
`results/` (per-archetype recall, cohort fairness, temporal stability,
calibration, plots). Large train windows are downsampled (`model_bench.max_train_rows`);
the test window is never sampled.

### 3.3 App / API
- **Local mode**: `app/` uses `FinanceFacts` directly (`config.yaml`).
- **Service mode**: `docker compose` runs the FastAPI facts service (`:8000`)
  and the app as its client (`FINSIGHT_API_URL`); if unreachable, the app
  degrades to local facts with a visible warning.
- Both bootstrap missing data/models on first start via `docker-entrypoint.sh`.

### 3.4 Fraud scoring
`tools.py` blends rule score + supervised probability + isolation-forest score
(configurable weights) → `risk_score`; the Fraud Detection page ranks
transactions above a sensitivity threshold and renders native LightGBM TreeSHAP
explanations (`pred_contrib`).

## 4. Configuration surface

| File / env | Purpose |
|---|---|
| `config.yaml` | data, model_bench, risk blend, budgets, digest, agent pricing |
| `.streamlit/config.toml` | Streamlit theme + server |
| `pyproject.toml` / `requirements.lock` | package + pinned deps (lockfile drift gated in CI and pre-push) |
| `FINSIGHT_API_URL` / `FINSIGHT_API_KEY` | service-mode wiring / shared secret |
| `ANTHROPIC_API_KEY` | LLM agent (optional; offline narrator otherwise) |
| `APP_PASSWORD` | demo-grade auth gate (optional) |
| `DIGEST_SLACK_WEBHOOK` / `DIGEST_SMTP_PASSWORD` | digest delivery secrets (never in config.yaml) |

## 5. Persistence

| Artifact | Location | Note |
|---|---|---|
| Demo ledger | `data/transactions.csv` (gitignored) | regenerable via `make data` |
| Bench ledger | `data/transactions.parquet` (gitignored) | regenerable via `make data-bench` |
| SQLite | `data/transactions.db` (gitignored) | scored cache, self-heals by fingerprint |
| Model bundles | `model_bench/*.joblib` (gitignored) | regenerable via `make train` |
| Benchmark evidence | `model_bench/results/` (gitignored) | CSV + PNG per §3.2 |
| Tracked metadata | `model_bench/best_model_metadata.json`, `SEED_COUNTER` | retrain job diffs metrics / advances seed |

## 6. Deployment

- **Docker Compose** (`docker-compose.yml`): `api` + `finsight` services,
  shared artifacts volume, bounded restarts, resource limits, non-root user.
- **CI/CD** (`.github/workflows/`): `ci.yml` (fast suite on `tiny` tier;
  nightly `benchmark-nightly` job with bench generation/training wall-clock
  gates; slow suite), `retrain.yml` (weekly retrain PR), `digest.yml`
  (scheduled digest).
- **Git hooks** (`.githooks/pre-push`): lockfile drift check, same script as CI.

## 7. Key design decisions (short form)

1. **PR-AUC first** — with ~0.1–1% fraud, accuracy is meaningless; the
   benchmark optimizes mean CV PR-AUC with honest per-archetype reporting.
2. **Temporal integrity** — backward-looking features only (leakage-guard
   tests), temporal splits, never-sampled test windows.
3. **Honest evidence** — adversarial patterns (mimicry, seasonal mimicry) are
   deliberately imperfectly caught and reported as such.
4. **Config-driven** — every tunable (blend weights, budgets, tiers) lives in
   `config.yaml`, validated by `config_schema.py`.
5. **Reproducible synthetic data** — SeedSequence-derived per-persona RNG
   streams; `--seed` gives byte-identical output.
