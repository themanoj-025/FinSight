# FinSight Agent — Startup Flow

FinSight Agent ships two app modes (Streamlit single-process and FastAPI
service mode) plus a CLI. All boot paths depend on `generate_data.py` +
`config.yaml` at the repo root (Docker COPYs them; `docker-entrypoint.sh`
runs data generation before the app).

## Container boot (`docker-entrypoint.sh`)

1. `python generate_data.py --config config.yaml` — if the dataset is
   missing/stale, generates the tiered synthetic ledger (`finance_agent.datagen`).
2. `python -m model_bench.train_and_compare` (or a lighter load step) —
   ensures the risk-model bundle exists; signature-verified on load via
   `finance_agent.bundle_security`.
3. Launch target:
   - **Streamlit mode** — `streamlit run app/Home.py` (default; pages in
     `app/pages/` auto-register).
   - **Service mode** — `uvicorn finance_agent.api:app` (FastAPI `/api/v1/*`,
     consumed by `app/api_client.py`).

## Streamlit mode import chain

1. `app/Home.py` → `app/common.py` — sets up styling, auth, caching; lazily
   imports `finance_agent.tools` / `finance_agent.agent` (heavy LLM imports
   deferred until a page needs them).
2. Pages (`1_Dashboard` … `7_Trust_Transparency`) call `finance_agent.*`
   facades; `6_Settings.py` can re-run `generate_data.py` in-process.
3. `finance_agent.storage` initializes the SQLite ledger (`data/transactions.db`)
   with schema migrations on first use.

## Service mode import chain

1. `finance_agent.api` builds the FastAPI app; per-request dependency wiring
   constructs `FinanceAgent` with the configured model (allowlist from
   `constants.DEFAULT_MODEL`).
2. Tool routes (`/api/v1/*`) dispatch through `finance_agent.tools.FinanceFacts`.
3. Observability hooks (`finance_agent.observability`) attach logging + metrics.

## CLI mode

`python -m finance_agent` → `cli.py` dispatches subcommands (data, digest,
report, chat); `--config config.yaml` respected everywhere.

## What must exist at startup

- `config.yaml` (+ env keys per `docs/technical/Deployment.md`)
- `data/transactions.db` (auto-created by `storage.py`) and generated CSVs
- `model_bench/*.joblib` + `.sig` (bundled; verified on load)
- `config.yaml`-declared LLM provider credentials (optional in offline mode —
  `agent.py` falls back to the offline narrator)
