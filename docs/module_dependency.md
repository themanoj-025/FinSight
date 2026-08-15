# FinSight Agent — Module Dependency Map

This is the module-level companion to `architecture.md` §2 (layered model).
Dependencies point **strictly downward**: `app/` → `finance_agent` →
`model_bench`.

## Layer-to-layer edges

```
app/ (Streamlit pages + common.py + api_client.py)
  → finance_agent.tools, finance_agent.agent, finance_agent.storage,
    finance_agent.report, finance_agent.pdf_export, finance_agent.observability,
    finance_agent.constants, finance_agent.api
  → generate_data.py  (Settings page re-runs data generation)
  → config.yaml        (bootstrap, by path)

finance_agent/api.py   → tools, storage, rules, features, observability
finance_agent/cli.py / __main__.py → agent, tools, storage, digest, report
finance_agent/agent.py → tools (FinanceFacts hub), observability
finance_agent/tools.py → rules, features, storage, retrieval, merchants,
                         model_bench.models  ← ONLY sanctioned bridge to MLOps
finance_agent/datagen.py → personas, merchants, fraud_patterns, storage,
                           config_schema, observability
finance_agent/report.py / pdf_export.py / digest.py → storage, tools, constants
finance_agent/rules.py / features.py → constants, config_schema
finance_agent/storage.py → config_schema, constants
finance_agent/bundle_security.py → signature verification for model artifacts

model_bench/models.py   → (leaf, plus .joblib artifacts)
model_bench/evaluate.py / hpo.py / canary.py → models
model_bench/train_and_compare.py → models, evaluate, datagen
```

## Cross-cutting utilities (no dependents)

- `finance_agent/constants.py` — model allowlist, defaults; imported everywhere.
- `finance_agent/observability.py` — logging/metrics hooks; imported by app,
  api, agent, datagen.
- `finance_agent/config_schema.py` — config validation; imported by datagen,
  rules, features, storage, cli.
- `finance_agent/retrieval.py` — RAG over the generated dataset; used by tools.

## Rules

- **No upward imports** — `finance_agent.*` never imports `app.*`; `model_bench`
  never imports `finance_agent` internals (except via the sanctioned
  `tools` → `model_bench.models` bridge, one-way).
- **No circular imports** — verified by AST scan (`docs/project/analysis_report.md` §5).
- **Entry scripts stay at root by contract** — `generate_data.py` and
  `config.yaml` are referenced by absolute path in `Dockerfile`,
  `docker-entrypoint.sh`, CI and 4 workflow files; they must not move.

## External dependencies

FastAPI (service mode) · Streamlit (UI) · pandas/pyarrow (data) ·
scikit-learn + joblib (models) · SQLite (stdlib persistence) · an LLM provider
for `agent.py` (configurable per `config.yaml`).
