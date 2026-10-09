# FinSight Agent — File Move Ledger

## This pass (2026-08-11)

| Old path | New path | Category | Reason | Risk | Verified |
|---|---|---|---|---|---|
| `docs/migration_summary.md` | `docs/migration/migration_summary.md` | Meta/docs | Consolidate migration records under `docs/migration/` per enterprise standard | Low (docs only) | ✅ `git mv` preserved history; 2 inbound tree references updated (`docs/folder_structure.md` §2, `PROJECT_OVERVIEW.md`) |

## Prior pass (2026-08-08 v5.0 modernization)

The v5.0 modernization moved application files into the current canonical
layout. Its complete ledger is preserved **in-repo** in two places:

- `docs/migration/migration_summary.md` §3 — move log (old path | new path | reason | mechanism)
- `docs/folder_structure.md` §3 — file-move log + §4 root-allowlist compliance

Representative entries from that pass:

| Old path | New path | Reason |
|---|---|---|
| `conftest.py` (root) | `tests/conftest.py` | Canonical pytest location; only non-entry-point root file |
| (flat root modules) | `finance_agent/**` | Domain cohesion under the core package |
| (scattered UI) | `app/**` | Streamlit presentation layer |
| (scattered MLOps) | `model_bench/**` | Model benchmarking + artifacts |
| (operational scripts) | `scripts/**` | One tool per canonical home |

## Non-moves (documented decisions)

| Path | Decision | Reason |
|---|---|---|
| `generate_data.py`, `config.yaml` (root) | keep | Entry contract: referenced by absolute path in Dockerfile, `docker-entrypoint.sh`, CI and 4 workflows |
| `status/health.log` | keep tracked | Deliberate: doubles as the static status page (see `.gitignore` comment D.2) |
| `model_bench/SEED_COUNTER` | keep tracked | Deterministic-seed counter for reproducible generation |
| `.hypothesis/`, `.mypy_cache/`, `.coverage`, `*.egg-info/`, `build.log`, `loadtest/results_*` | leave (untracked) | Build/cache artifacts, correctly gitignored |
