# Analysis Report — Repository Inventory & Classification

Date: 2026-08-08 · Scope: entire FinSight Agent repository · Method: file-by-file
read + AST import-graph scan + content-hash duplicate scan + reference scan.

This report is the written inventory required by Phase 1–2 of the repository
modernization pass. It lists every top-level module, its purpose, its
classification, and its intra-package dependencies. Nothing in this report
changes behavior — it is the evidence base for the (deliberately minimal)
restructuring documented in [`docs/migration/migration_summary.md`](../migration/migration_summary.md).

---

## 1. Stack overview

| Dimension | Value |
|---|---|
| Language / runtime | Python ≥ 3.10 (CI matrix 3.10/3.12, image 3.12-slim) |
| Package manager | `pyproject.toml` (setuptools) + pinned `requirements.lock` (uv) |
| Application | Streamlit app (`app/`) + FastAPI facts service (`finance_agent/api.py`) |
| Packaging | `finance_agent*` + `model_bench*` (editable install) |
| Lint / type / test | ruff (E,F,W,I,UP,B,SIM) · mypy · pytest (fast / slow / data_realism markers) |
| CI | GitHub Actions: `ci.yml` (PR/push), `retrain.yml` (weekly), `digest.yml` (scheduled) |

## 2. Top-level inventory (root)

| Path | Purpose | Classification |
|---|---|---|
| `app/` | Streamlit front end (Home + 6 pages, shared `common.py`, API client) | Presentation |
| `finance_agent/` | Core package — data generation, facts, rules, features, model scoring, agent, API, CLI | Domain / Application |
| `model_bench/` | Benchmark harness — models, evaluation, training, per-archetype recall | Tests-of-record / MLOps |
| `tests/` | pytest suite (17 test files + `conftest.py`) | Tests |
| `docs/` | Documentation suite (see §4) | Docs |
| `scripts/` | `check_docs_consistency.py`, `check_lockfile.sh` | Infrastructure |
| `.github/` | CI/CD workflows + issue templates + dependabot | Infrastructure |
| `.githooks/` | pre-push lockfile-drift hook | Infrastructure |
| `.streamlit/` | Streamlit theme/server config | Configuration |
| `generate_data.py` | Data-generation CLI **and** importable module (`from generate_data import generate`) | Entry point |
| `config.yaml` | Central configuration (data, model_bench, risk, budgets, digest, agent) | Configuration |
| `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh` | Containerization + shared bootstrap | Infrastructure |
| `Makefile` | Task runner (data/train/run/api/test/lint/typecheck/docs-check) | Infrastructure |
| `pyproject.toml`, `requirements.lock` | Package metadata + pinned deps | Configuration |
| `README.md`, `CHANGELOG.md`, `LICENSE`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `SECURITY.md` | Project metadata | Docs |
| `.gitignore`, `.gitattributes`, `.dockerignore` | VCS / build metadata | Configuration |

## 3. Finance_agent package (domain & application)

Layers are strictly separated (documented in `finance_agent/__init__.py`):
**Facts** (`tools`, `rules`, `features`, `storage`, `datagen`) → **Reasoning**
(`agent`) → **Presentation** (`cli`, `report`, `api`, `digest`, `pdf_export`).

| Module | Purpose | Depends on (intra-package) | Classification |
|---|---|---|---|
| `constants.py` | Canonical enums: transaction types, account types, fraud archetypes, model allowlist | — (leaf) | Domain |
| `merchants.py` | Merchant catalog, regions, haversine distances | — (leaf) | Domain |
| `personas.py` | Persona archetypes + parameter sampling | `merchants` | Domain |
| `fraud_patterns.py` | 15 difficulty-graded fraud/anomaly generators + hard negatives | `merchants`, `personas` | Domain |
| `config_schema.py` | Config validation/normalization | — (leaf) | Configuration |
| `features.py` | Model features (causal, leakage-guarded) | `constants`, `merchants` | Application |
| `datagen.py` | Vectorized tiered data generator (tiny/demo/bench) | `constants`, `merchants`, `personas`, `fraud_patterns` | Domain |
| `rules.py` | Rule-based detectors (balance drain, duplicates, spikes) | `constants` | Application |
| `storage.py` | SQLite persistence + schema migrations | — (leaf) | Data access |
| `tools.py` | `FinanceFacts` hub — scoring, summaries, tools | `constants`, `config_schema`, `features`, `rules`, `storage`, `model_bench.models` | Application |
| `report.py` | Monthly report builder | `tools` | Application |
| `pdf_export.py` | PDF rendering of the report | `report` | Presentation |
| `digest.py` | Weekly digest builder/delivery | `constants`, `report`, `rules`, `tools` | Application |
| `agent.py` | LLM tool-use agent + offline narrator, usage tracking | `constants`, `tools` | Application |
| `api.py` | FastAPI facts service (v1 routes) | `tools` | API/interface |
| `cli.py` | `finsight` CLI (ask/chat/report/digest) | `agent`, `digest`, `pdf_export`, `report` | API/interface |
| `__main__.py` | `python -m finance_agent` entry | `cli` | Entry point |
| `__init__.py` | Version + layer docs | — | — |

## 4. Documentation suite

| Path | Purpose |
|---|---|
| `docs/DataGeneration.md` | Three-tier generator, personas, fraud library, reproducibility |
| `docs/KNOWN_LIMITATIONS.md` | Honest limitations (synthetic data, auth, sampled training…) |
| `docs/technical/` | TechSpec, Schema, API, Deployment, SecurityAndCompliance, Testing |
| `docs/design/` | AppFlow, Design system |
| `docs/product/` | PRD |
| `docs/project/` | ImplementationPlan, RiskRegister, Rules, Tracker (+ this report) |
| `docs/reference/` | Glossary |
| `docs/assets/` | Screenshots referenced by README / design docs |

## 5. Dependency graph (intra-package, acyclic)

```
finance_agent/  module → intra-package dependencies (→ = imports)
  datagen        → constants, merchants, personas, fraud_patterns
  fraud_patterns → merchants, personas
  personas       → merchants
  features       → constants, merchants          (no dependency on datagen)
  rules          → constants
  tools          → config_schema, constants, features, rules, storage, model_bench.models   ← hub
  agent          → constants, tools
  api            → tools · report → tools · pdf_export → report
  digest         → constants, report, rules, tools · cli → agent, digest, pdf_export, report

app/ (strictly downstream of finance_agent):
  common         → api_client + finance_agent.{agent, constants, storage, tools}
  Home / pages   → common

model_bench/:
  train_and_compare → finance_agent.features + model_bench.{evaluate, models}
  evaluate / models → no intra-package dependencies
```

- Leaf modules: `constants`, `merchants`, `config_schema`, `storage`.
- Hub: `tools.py` (the only module that touches `model_bench.models`).
- `app/` is strictly downstream of `finance_agent` (one direction).
- **No circular imports** and no cross-package imports inside `finance_agent`'s
  leaf layer — verified by AST scan and by the import-clean test suite.

## 6. Classification tally

| Category | Count (top-level) | Examples |
|---|---|---|
| Entry point | 3 | `generate_data.py`, `__main__.py`, `cli.py` |
| Configuration | 5 | `config.yaml`, `pyproject.toml`, `.streamlit/`, `config_schema.py` |
| Domain / business logic | 8 | `datagen`, `personas`, `fraud_patterns`, `merchants`, `tools`, `agent`, `rules`, `report` |
| Data access | 2 | `storage.py`, `features.py` |
| API / interface | 3 | `api.py`, `cli.py`, `app/api_client.py` |
| Presentation | 9 | `app/` pages + `common.py`, `pdf_export.py` |
| Cross-cutting | 3 | auth gate (`common.require_auth`), X-API-Key gate (`api.py`), logging |
| Infrastructure | 11 | Docker*, CI, scripts, Makefile, .githooks |
| Tests | 17 | `tests/*.py` |
| Docs | 24 | `docs/**` + root metadata |

## 7. Findings summary (evidence for Phase 3)

| Scan | Method | Result |
|---|---|---|
| Duplicate files | SHA-256 content hash over all tracked source/doc text files (excluding `.github/` and gitignored artifact dirs) | **0 duplicate-content groups** |
| Empty files | size == 0 walk | **none** |
| Dead module-level symbols | AST scan, 126 symbols, internal+external refs | **0 dead** (all 16 flagged candidates verified used) |
| Unused imports/vars | ruff F401/F841 (project linter, enforced in CI) | clean |
| Duplicated helpers | grep for shared helper names (`round2`, `haversine_miles`, `_focal`, `expense_rows`) | each defined once |
| Hardcoded secrets | regex scan (Anthropic key shapes, `key=`, `password=`, private keys) | **none** — only placeholder/env-var reads |
| Silent `except: pass` | grep audit | none (the 3 best-effort cache clears in `app/common.py` are commented and intentional) |
| Stale docs | link/reference audit vs. tracked files (`check_docs_consistency.py` 15/15) | clean |
| AI scaffolding (TODO/FIXME/placeholder) | grep `-i` | **none** — only legit UI/DB placeholders |

No file met the evidence bar for deletion (see Phase 3 workflow in the
migration summary). The repository was already in a maintainable state.
