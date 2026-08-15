# FinSight Agent — Package & Module Inventory

## Core package: `finance_agent`

| Module | Responsibility |
|---|---|
| `__init__.py` / `__main__.py` | Package marker + CLI entry (`python -m finance_agent`) |
| `cli.py` | Subcommand dispatcher (data, digest, report, chat) |
| `constants.py` | Model allowlist, defaults, shared literals |
| `config_schema.py` | `config.yaml` validation |
| `datagen.py` | Tiered deterministic synthetic ledger generator (tiny/demo/bench) |
| `personas.py` | Focal-user personas for data generation |
| `merchants.py` | Merchant catalog used by datagen + tools |
| `fraud_patterns.py` | 15 fraud archetypes injected into the ledger |
| `rules.py` | Rule-based anomaly detectors |
| `features.py` | Model feature construction |
| `storage.py` | SQLite persistence + schema migrations |
| `tools.py` | `FinanceFacts` tool hub — the app-facing fact/query API |
| `agent.py` | LLM tool-use loop + offline narrator |
| `report.py` / `pdf_export.py` | Monthly report builder + PDF rendering |
| `digest.py` | Daily digest generation |
| `api.py` | FastAPI service (`/api/v1/*`) |
| `alerts.py` | Alert evaluation/push |
| `retrieval.py` | RAG retrieval over the generated dataset |
| `observability.py` | Logging/metrics hooks |
| `bundle_security.py` | Signature verification of model bundles |

## Presentation: `app/` (Streamlit)

| Module | Responsibility |
|---|---|
| `Home.py` | Landing page (entry) |
| `common.py` | Shared auth, styling, caching, bootstrap |
| `api_client.py` | Client for FastAPI service mode |
| `pages/1_Dashboard.py` … `7_Trust_Transparency.py` | Feature pages (transactions, fraud, chat, reports, settings) |

## MLOps: `model_bench/`

| Module | Responsibility |
|---|---|
| `models.py` | Model definitions (risk model bundle) |
| `evaluate.py` | Evaluation harness + report artifacts |
| `hpo.py` | Hyperparameter optimization |
| `canary.py` | Canary checks on retrained models |
| `train_and_compare.py` | End-to-end train-and-compare entry |
| `results/` | Charts + CSVs (gitignored except tracked metadata) |
| `*.joblib` + `.sig` | Bundled trained artifacts + signatures |

## Tests: `tests/` (25 modules)

`test_agent`, `test_alerts`, `test_api`, `test_api_client`, `test_app_smoke`,
`test_canary`, `test_config`, `test_data_realism`, `test_digest`,
`test_evaluate`, `test_features`, `test_fraud_patterns`, `test_generate_data`,
`test_golden_answers`, `test_hpo`, `test_merchants`, `test_models`,
`test_observability`, `test_pdf_export`, `test_properties`, `test_resilience`,
`test_retrieval`, `test_rules`, `test_storage`, `test_tools` + `conftest.py`.

## Non-package trees

| Path | Purpose |
|---|---|
| `scripts/` | `accessibility_check.py`, `check_docs_consistency.py`, `check_lockfile.sh`, `contract_fuzz.py`, `export_openapi.py`, `generate_secrets.sh`, `loadtest_check.py`, `slo_check.py`, `verify_secrets.py` |
| `loadtest/` | Locust load tests (results gitignored) |
| `a11y/` | Accessibility scan report |
| `deploy/` | Caddy / nginx example configs |
| `data/` | Generated ledger (CSV, parquet, SQLite) |
| `reports/` | Monthly report outputs (md/pdf) |
| `status/` | Health-probe log (deliberately tracked as static status page) |
| `docs/` | Full documentation suite (see `docs/folder_structure.md` §2) |
