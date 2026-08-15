# Folder Structure

Canonical layout of the FinSight Agent repository after the modernization pass,
plus the file-move log. The structure follows the target architecture in the
modernization prompt ("adapt, don't force-fit"): a Python app/ package layout
with strictly separated layers, feature-cohesive modules, and a clean root.

## 1. Current tree (canonical)

```
finsight-agent/
├── app/                        # Streamlit front end (presentation)
│   ├── Home.py                 #   entry: landing page
│   ├── common.py               #   shared: auth, styling, caching, bootstrap
│   ├── api_client.py           #   FastAPI service-mode client
│   └── pages/                  #   Dashboard, Transactions, Fraud Detection,
│                               #   Ask the Agent, Reports, Settings
├── finance_agent/              # Core package (domain + application)
│   ├── __init__.py  __main__.py  cli.py
│   ├── constants.py  config_schema.py
│   ├── datagen.py  personas.py  merchants.py  fraud_patterns.py
│   ├── rules.py  features.py  storage.py  tools.py
│   ├── agent.py  report.py  pdf_export.py  digest.py  api.py
├── model_bench/                # MLOps: benchmark + artifacts
│   ├── models.py  evaluate.py  train_and_compare.py
│   ├── best_model_metadata.json  SEED_COUNTER   (tracked metadata)
├── tests/                      # pytest suite (incl. conftest.py)
│   └── test_*.py (17 files)
├── docs/                       # documentation suite (see below)
├── scripts/                    # check_docs_consistency.py, check_lockfile.sh
├── .github/                    # CI/CD (ci.yml, retrain.yml, digest.yml) + templates
├── .githooks/                  # pre-push hook (lockfile drift)
├── .streamlit/                 # Streamlit theme config
├── generate_data.py            # ENTRY POINT + importable module (must stay at root)
├── config.yaml                 # central configuration (referenced everywhere)
├── Dockerfile  docker-compose.yml  docker-entrypoint.sh   # container tooling
├── Makefile                    # task runner
├── pyproject.toml  requirements.lock
├── README.md  CHANGELOG.md  LICENSE  CODE_OF_CONDUCT.md  CONTRIBUTING.md  SECURITY.md
└── .gitignore  .gitattributes  .dockerignore
```

## 2. Docs tree

```
docs/
├── DataGeneration.md           # tiers, personas, fraud library, reproducibility
├── KNOWN_LIMITATIONS.md        # honest limitations
├── architecture.md             # ← new: canonical architecture reference
├── folder_structure.md         # ← new: this file
├── migration_summary.md        # ← modernization pass report
├── migration/                  # migration records (this file moved here 2026-08-11)
├── assets/                     # screenshots (dashboard, fraud, chat) + README
├── design/  product/  project/  reference/  technical/
└── project/
    └── analysis_report.md      # ← new: full inventory + classification
```

## 3. File-move log (modernization pass)

| Old path | New path | Reason | Mechanism |
|---|---|---|---|
| `conftest.py` | `tests/conftest.py` | Canonical pytest location; removes the only non-entry-point file from the root; keeps `boot_api_server` helper and the `api_server` session fixture scoped with the suite | `git mv` (97% rename similarity — history preserved) + 1-line `ROOT` fix (parent → parent.parent) |

No other files moved. Every other root file is either an entry point
(`generate_data.py`), standard project metadata (README/LICENSE/…), container
tooling coupled to the root Dockerfile (`Dockerfile`, `docker-compose.yml`,
`docker-entrypoint.sh`), or top-level configuration (`config.yaml`,
`.streamlit/`) — all permitted by the root allowlist.

## 4. Root allowlist compliance

| Root entry | Status |
|---|---|
| `generate_data.py` | ✔ entry point (also imported by tests/scripts — moving would break public imports) |
| `config.yaml` | ✔ top-level configuration (referenced by Makefile, CI, Docker, app, CLI) |
| `Dockerfile` / `docker-compose.yml` / `docker-entrypoint.sh` | ✔ container tooling (couples to `/app/docker-entrypoint.sh` in both; Docker build unverifiable on this host → kept, flagged) |
| `Makefile`, `pyproject.toml`, `requirements.lock`, `.gitignore`, `.gitattributes`, `.dockerignore` | ✔ standard metadata |
| `README.md`, `CHANGELOG.md`, `LICENSE`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `SECURITY.md` | ✔ standard metadata |
| `.github/`, `.githooks/`, `.streamlit/`, `app/`, `finance_agent/`, `model_bench/`, `tests/`, `docs/`, `scripts/` | ✔ top-level folders |

Result: **no stray files remain at root**.

## 5. Why not more restructuring?

The modernization prompt's target architecture (e.g. `api/`, `core/`, `domain/`,
`services/`, `repositories/` subpackages) is deliberately **not** force-fit onto
`finance_agent/`. Evidence (see `analysis_report.md` §5): the package is already
strictly layered and acyclic, module sizes are within reason, and moving modules
would change import paths that are treated as public API by `app/`, tests,
`model_bench`, and the CLI. Reorganizing into artificial subpackages would add
churn without a maintainability win — consistent with the prompt's own rule:
"don't over-engineer … adapt to the actual stack, do not force-fit."
