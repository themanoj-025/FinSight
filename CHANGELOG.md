# Changelog

All notable changes to **FinSight Agent** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security audit closed — secrets runbook, bundle-key rotation guard, auth decision (audit series finale)

- **Secrets provisioning runbook (§1a)** — `scripts/generate_secrets.sh` (mints
  strong `APP_PASSWORD` / `FINSIGHT_API_KEY` / `FINSIGHT_BUNDLE_KEY`, never
  persists them), `make generate-secrets`, and `make verify-secrets`
  (`scripts/verify_secrets.py`): self-checks a live deployment's gates —
  `/api/*` returns 401 without the key and 200 with it, plus an app
  reachability probe with an explicit manual password-prompt checklist.
  DEPLOY.md §3 now links the script instead of saying "choose strong values".
- **Bundle-signing key rotation (§2)** — new `ensure_bundle_verified()`
  fail-fast startup guard in `finance_agent/bundle_security.py`: when
  `FINSIGHT_BUNDLE_KEY` is set, a bundle whose signature doesn't verify
  against it aborts the API at boot with a `BundleSignatureError` naming the
  fix (re-sign on the target) instead of silently serving rule-only scores.
  The public demo default key is documented as local-development-only.
  Test: a bundle signed with the demo default is rejected loudly under a real
  key. Verified end-to-end: mismatched key → boot refused with the fix message.
- **Flaky property test fixed for real (§3)** — `test_fraud_rate_upper_band_holds_for_meaningful_pools`
  previously flaked on Python 3.14 (seed=454, n_bg=20 → 6.33% vs a 6% band).
  A measured 500-seed sweep proved n_bg=20 is single-event-noise dominated
  (max 8.78%, 24/200 seeds over 6%) while n_bg≥30 never exceeds 6% (max
  5.78% over 200 seeds). The pool floor moves 20 → 30 with the measured
  table in the docstring, and a new `test_fraud_rate_stays_bounded_for_noisy_tiny_pools`
  covers the tiny-pool corner with an explicitly wide, sanity-only ceiling.
  Verified deterministic across 500 hypothesis examples on this machine.
- **Reverse-proxy CSP/TLS templates (§4)** — `deploy/Caddyfile.example` +
  `deploy/nginx.conf.example` (CSP, HSTS, baseline headers, Streamlit/Swagger
  inline-script caveats, optional per-IP caps). Linked from DEPLOY.md §9;
  KNOWN_LIMITATIONS/`SecurityAndCompliance.md` now say "template provided at
  `deploy/`, adopt or adapt" instead of bare "deferred to your reverse proxy".
- **Real-auth question resolved explicitly (§5)** — `SecurityAndCompliance.md`
  and KNOWN_LIMITATIONS §3 now record the demo-grade shared-password gate as a
  **permanent, deliberate design choice** (synthetic-data demo, no multi-tenant
  need), not an open item; if the codebase is ever repurposed, real auth gets
  its own spec (FastAPI Users + JWT recommended).

### Quality gates closed (F.3/F.4/F.5 + D.4 + contract fixes)

- **Contract fuzzing (F.3)** — `scripts/contract_fuzz.py` + `make contract-fuzz` + a
  nightly/on-demand `contract-fuzz` CI job: schemathesis fuzzes every operation in the
  committed `docs/technical/openapi.v1.json` against a live API (deterministic seed;
  `POST /api/v1/reload` excluded by design). First run surfaced **14 real contract bugs**,
  all fixed: 422 `detail` shape now matches the documented `HTTPValidationError` (was a
  bare string, not an array), nullable query params accept `null`, `user` is an explicit
  enum of the configured focal users (was any string → 422 on unknown users), and
  `/metrics` documents its actual `text/plain` content type. Fuzz pass: 494 cases, green.
- **Load testing vs SLOs (F.5)** — `loadtest/locustfile.py` + `scripts/loadtest_check.py` +
  `make loadtest` + a nightly/on-demand `load-test` CI job (50 users/120 s on the cached-
  facts endpoints, warmup pass first, CSV/HTML/report artifacts uploaded). The first run
  exposed warm-path violations of the 200 ms p95 SLO, fixed in `tools.py`: month labels
  memoized once per `FinanceFacts` (was `pd.to_datetime` over the full ledger ~3× per
  request — `monthly-summary` 420 ms → 8 ms), the similar-transactions helper no longer
  pays for per-row TreeSHAP it never reads (`_top_risk_row_index` 1.3 s → ~5 ms), and
  `_shap_explanations` now degrades gracefully (KeyError catch) instead of 500-ing on a
  stale-store fingerprint collision. The finishing piece is **API-level response caching**
  (the facts endpoints memoize their JSON-safe result per params and invalidate on
  `POST /api/v1/reload`, matching the documented startup-snapshot semantics) — the first
  full 50-user run had measured p95 of 1.3–3.7 s with `risk-scored` (per-request SHAP)
  erroring outright; the full profile now measures **p95 17–18 ms, 0% errors across all
  12 endpoints** (~20k requests in 120 s).
- **Accessibility + mobile render (F.4)** — `scripts/accessibility_check.py` +
  `make a11y` + a weekly `accessibility` workflow: Playwright + axe-core over all 8 pages
  (zero critical/serious violations), a 375px mobile-overflow check on the two layout-
  dense pages, and a rendered-exception-box detector. Optional workflow by design. First
  CI run flagged the root page's sidebar `aria-expanded` on a role-less `<section>` —
  Streamlit-framework markup, not app code (verified it only appears on the root view);
  the check now excludes that one selector with a transparent report NOTE, and the first
  dispatched run is green (8/8 pages, zero critical/serious).
- **Mutation-testing CI fixed (D.4)** — the `mutation` job's `mutmut results --all` used
  flag syntax mutmut 3.7 rejects (it is a value option); now `--all true`, and the parser
  handles every status (killed/survived/timeout/suspicious/skipped) with the documented
  kill-rate formula and a 55% regression floor.
- **API deployment fix** — `finance_agent/api.py` now exports a module-level `app` so
  `uvicorn finance_agent.api:app` (used by `make api`, docker-compose, fly.toml,
  `docker-entrypoint.sh`, and DEPLOY.md) actually boots.
- **CI job fixes** — `status.yml` creates `status/` before `git add` on the URL-unset path;
  `retrain.yml` installs dev extras so the realism gate's pytest run finds pytest.
- **Test fix** — `test_observability.py` asserted the `X-Request-Id` header via
  case-sensitive dict lookup; HTTP headers are case-insensitive, so the assertion now
  matches `x-request-id` (the middleware was always correct).
- **Demo script (G.2)** — `docs/DEMO_SCRIPT.md`: a timed 90-second shot list for the demo
  video, ready to record once a live URL exists.

### Repo modernization (structure & hygiene, zero behavior change)

- **Root cleanup** — moved `conftest.py` into `tests/` (canonical pytest location;
  `git mv`, history preserved, `ROOT` path fix); root now holds only entry points,
  standard metadata, container tooling, and configuration. No deletions: hash-based
  duplicate scan (0 groups), AST dead-code scan (0 dead symbols), empty-file scan
  (none), secret scan (clean), scaffolding scan (clean).
- **Follow-up dead-code cleanup** — removed 6 zero-reference legacy symbols:
  `_u01` / `archetype_of` (`finance_agent/personas.py`), `region_distance_miles`
  (`finance_agent/fraud_patterns.py`), and `SALARY_AMOUNT` / `RENT_AMOUNT` /
  `SAVINGS_AMOUNT` / `TYPE_BY_CATEGORY` / `CREDIT_TYPES` (`generate_data.py`, the
  last a duplicate of the canonical `finance_agent.constants.CREDIT_TYPES`); the
  no-op `SALARY_AMOUNT` monkeypatch in `tests/test_generate_data.py` was dropped.
  Zero behavior change — all gates green (ruff, mypy, 202 fast tests, docs-check).
- **New canonical docs** — `docs/architecture.md`, `docs/folder_structure.md`,
  `docs/migration/migration_summary.md` (with Needs-Human-Review list), and
  `docs/project/analysis_report.md` (full inventory + classification + dependency
  graph). README documentation index extended.

### Added (stretch, v0.2)

- **Budget goal tracker** — per-category monthly goals in `config.yaml
  budgets.monthly`, a new `budget_status` facts tool (agent tool + `/api/v1/budget-status`
  endpoint + monthly report section), and Dashboard progress bars with over-goal
  callouts. Goals are tracking-only: they never block or alter transactions.
- **Multi-user support** — `data.focal_users` generates a full, internally-
  consistent ledger per user (salary, rent, subscriptions, anomalies); the app
  sidebar switches the focal user across all pages, `FinanceFacts(focal_user=…)`
  selects per-user data, and the API accepts `?user=U_X`. The benchmark keeps
  SHAP-capable LightGBM when it is statistically indistinguishable from the CV
  leader (explainability tie-break, recorded in `best_model_metadata.json`).
- **Weekly digest** — `finance_agent/digest.py` builds a Markdown summary of
  the trailing 7 days and delivers it via opt-in Slack Incoming Webhook
  (`DIGEST_SLACK_WEBHOOK`) and/or SMTP email (`digest.email`), both stdlib-only;
  `make digest` / `python -m finance_agent digest`; scheduled weekly `digest.yml`
  workflow; graceful file-only fallback when no channel is configured.

### Added (data-gen v2 — Data-Scale & Realism)

- **Three generation tiers** — `--tier tiny|demo|bench` (`tiny` = legacy footprint for
  CI/tests, `demo` = app & README default, `bench` = multi-million-row Parquet ledger for
  `model_bench`); new `make data-tiny|data-demo|data-bench|train-bench|bench` targets;
  `bench` output is gitignored. `tiny` output shape is backward compatible.
- **Persona population model** (`finance_agent/personas.py`) — 6 archetypes (young
  professional, dual-income family, gig worker, retiree, recent graduate, small-business
  owner) with per-individual randomized parameters (Dirichlet category weights, income
  cadence/level, rent share, savings rate, 2–5%/yr raises).
- **15-pattern fraud/anomaly library** (`finance_agent/fraud_patterns.py`) — easy
  (balance drain, duplicate charge, spend spike), medium (card testing, slow balance drain,
  new-payee transfer, subscription creep, refund abuse), hard/adversarial (mimicry, account
  takeover, bust-out, seasonal mimicry), plus 3 hard negatives (life event, travel, rapid
  burst) — per-archetype labels and ~2% discovery-lag label realism.
- **Seasonality, drift & multi-account structure** — month-of-year spend multipliers,
  payday-aligned spending bursts, annual raises + 2.5% inflation, checking/savings/credit
  accounts with autopay + auto-savings flows, legitimate life events.
- **Geography & merchant taxonomy** (`finance_agent/merchants.py`) — ~300 named synthetic
  merchants with Zipfian popularity, ~36 regions with great-circle distances, and a
  category → subcategory → category_group hierarchy.
- **Vectorized generator** (`finance_agent/datagen.py`) — no `iterrows()`/`.apply(axis=1)` in
  the hot path, clamped cumulative-sum balances (Lindley recursion), SeedSequence
  per-persona reproducibility.
- **Causal v2 features** (`finance_agent/features.py`) — region distance, out-of-home region,
  first-seen merchant/payee, account-channel one-hots, weekend; credit debt clamped before
  `log1p` (fixed NaN-in-features bug that crashed LogisticRegression folds).
- **Benchmark v2 diagnostics** — per-archetype recall, cohort fairness, temporal stability,
  and calibration tables + charts in `model_bench/results/` and `best_model_metadata.json`;
  the app's Fraud page renders the new charts.
- **Data realism suite** (`tests/test_data_realism.py`, `slow`/`data_realism`) — balance
  invariants, seasonality, income drift, life events not fraud, fraud-rate band,
  multi-account structure, seed determinism at scale.
- **Docs** — new `docs/DataGeneration.md`; `docs/technical/Schema.md` regenerated for the
  29-column / 32-feature surface; `docs/KNOWN_LIMITATIONS.md` extended (label noise,
  synthetic merchants/regions, hand-designed adversarial patterns, bench-scale notes).
- **CI** — nightly benchmark job takes a dispatch `tier` input (demo/bench); a dedicated
  `data-realism` job times demo-tier generation against a **60s wall-clock budget** (Data-Gen
  §1) and runs the realism suite; the weekly retrain PR is blocked by the realism gate; the
  push-leg slow suite excludes the realism file.

### Added (stretch, v0.3)

- **Branded PDF export** — hand-rolled, stdlib-only PDF writer
  (`finance_agent/pdf_export.py`) with A4 layout, navy brand band, accent rules,
  styled tables, and "Page X of Y" footer. Zero new dependencies. Wired into
  CLI (`finsight report --pdf`), Makefile (`make report`), and Reports page
  (download button). WinAnsi sanitization maps emoji to text markers;
  deterministic output (same markdown → identical bytes).
- **Cost/observability dashboard** — per-session usage capture in
  `finance_agent/agent.py` (input/output tokens from Anthropic API, narrator
  estimates, latency, estimated cost from `agent.pricing` in `config.yaml`);
  Settings page tab with KPIs (total turns, tokens, cost, avg latency), per-call
  table, and reset button. Chat sidebar shows remaining budget + est. cost.

### Bench-tier scale: performance, defaults, and measured evidence

- **Generator hot-path fixes** — `sample_background` was O(n²) (re-spawning the whole
  `SeedSequence` child list inside the loop; 20k profiles took minutes — now O(n) and
  byte-identical output); the balance pass now sorts events once by (account, step) with a
  single stable `lexsort` instead of a full-array scan per account (O(accounts×events) →
  O(n log n)); the `_resolve_drains` pair loop and the background spend draws were
  vectorized. Measured: `--tier demo` ~18s (63k rows), `--tier bench` ~5 min
  (10,700,142 rows, 4 years, 200 focal + 20k background personas, 0.06% fraud, Parquet).
- **Bench tier defaults** — `TIER_DEFAULTS` now carries the bench window (1,460 days) and
  focal population (200 personas) so `generate_data.py --tier bench` alone produces the
  spec'd multi-year, multi-persona ledger (fraud rate lands at ~0.06%, inside the enforced
  band; previously 0.001% with 2 focal personas).
- **Vectorized region-distance feature** — `region_distance_miles` is now a matrix lookup via
  `pd.Categorical` codes instead of a per-row Python loop (byte-identical output; the bench
  ledger has 10.7M rows).
- **Sampled training, honest evaluation** — `model_bench.max_train_rows` (config, default
  600k) stratifies the temporal *train* window (all fraud positives kept); the temporal
  *test* window is never sampled. Recorded in `best_model_metadata.json`
  (`dataset`/`config`).
- **Memory-lean Parquet loading** — the benchmark reads Parquet with `dtype_backend="pyarrow"`
  (string columns stay arrow-backed; the object-dtype frame would need ~10GB for 10.7M rows)
  and converts numeric/bool columns back to native numpy dtypes.
- **Refreshed bench evidence** — `best_model_metadata.json` + `model_bench/results/` now carry
  the bench run: LightGBM selected (CV PR-AUC **0.828 ± 0.055**, holdout 0.728; Random Forest
  leads at 0.855 but is within 1 CV std, so the SHAP-capable model wins the documented
  tie-break). Per-archetype recall on the 2.7M-row holdout: easy/medium 0.74–1.00, adversarial
  mimicry 0.21 / seasonal-mimicry 0.48. Training wall-clock ~9.5 min. README badge + model
  table + per-archetype table updated; measured timings recorded in
  `docs/KNOWN_LIMITATIONS.md` §17.
- **Bench wall-clock CI gates** — `benchmark-nightly` now times bench-tier generation (≤ 900 s
  vs ~299 s baseline) and training (≤ 1800 s vs ~563 s baseline) when dispatched with
  `tier=bench`, failing the job on regression (mirrors the demo-tier 60 s gate in
  `data-realism`).

### Docs & repo hygiene

- Rewrote the README to a full 10/10 standard: badge suite, table of contents, features table,
  project-structure tree, usage + Make-target reference, documentation index, FAQ/troubleshooting,
  and roadmap.
- Refreshed the entire `docs/` suite (dates, statuses, corrected task counts, PRD feature list +
  open questions, Deployment env-var reference, API CLI surface, Glossary terms).
- Hardened git hygiene: `.gitignore` (build logs, coverage artifacts, dist), `.gitattributes`
  (binary/text patterns), CI `concurrency` + least-privilege `permissions`, retrain-workflow PR
  title fix, new `dependabot.yml`, issue-template config, and this `CHANGELOG.md`.

## [0.1.0] — 2026-08-07

Definition-of-done gates verified end-to-end (fast suite < 30 s, slow suite, ruff/mypy, greps,
`make docs-check`).

### Added

- **Deterministic synthetic ledger generator** (`generate_data.py`) — PaySim-style focal-user
  ledger with ~150 background accounts and injected, labeled anomalies (balance drain, duplicate
  charge, spending spike, fraud pairs, freelance income). Balances always chain correctly;
  reproducible from a fixed seed (`--seed 0` respected).
- **Audit-rule detectors** (`finance_agent/rules.py`) — pure, vectorized, explainable balance-drain,
  duplicate-charge, and spend-spike detectors plus a financial-health score.
- **Shared, leakage-free feature engineering** (`finance_agent/features.py`) — strictly
  backward-looking features used by both training and inference; enforced by
  `test_no_temporal_leakage`.
- **6-model benchmark with honest evaluation** (`model_bench/`) — temporal train/test split (no
  shuffle), 5-fold time-series cross-validation, mean ± std PR-AUC/ROC-AUC/F1 metadata, comparison
  charts, and a serialized winner consumed by the blended risk score.
- **Blended risk score** — `w_rules·rule + w_model·model + w_iforest·iforest` with weights read
  live from `config.yaml risk.blend`; renormalizes to rule-only when no model bundle is present.
- **Bounded Claude tool-use agent** (`finance_agent/agent.py`) — system-prompt guardrails,
  5-turn tool bound, per-session turn/token budgets, multi-turn history cap, and a per-call
  activity log proving grounding.
- **Offline narrator fallback** — deterministic, specificity-weighted routing table that answers
  the same questions with no API key; prompt-injection heuristics included.
- **Streamlit app with 7 pages** (`app/`) — Dashboard, Transactions, Fraud & Anomaly Detection,
  Ask the Agent, Reports, and Settings, all tested with real `AppTest` rendering.
- **Markdown monthly report** (`finance_agent/report.py`).
- **Config validation** (`finance_agent/config_schema.py`) — `ConfigError` names the bad key at
  load time.
- **Demo-grade auth gate** — `APP_PASSWORD` shared-password gate with a visible "DEMO MODE — NOT
  SECURED" banner when unset.
- **Per-transaction SHAP explanations** — native LightGBM TreeSHAP (`pred_contrib`) in the Fraud
  page; contributions + bias exactly equal the model's log-odds.
- **Versioned FastAPI facts API** (`finance_agent/api.py`) — `/api/v1` routes, OpenAPI at `/docs`,
  optional `X-API-Key` gate, `POST /api/v1/reload`; the Streamlit app becomes a real client via
  `FINSIGHT_API_URL` (stdlib-only `app/api_client.py`).
- **SQLite persistence layer** (`finance_agent/storage.py`) — `transactions` table synced from the
  CSV plus a **materialized** `risk_scores` table, hand-rolled migrations via `PRAGMA user_version`;
  makes the interactive risk scan a SQL point query.
- **CI/CD** — `.github/workflows/ci.yml` (lint, typecheck, fast + slow tests with coverage gate,
  docs-code consistency, lockfile-drift, `pip-audit`, install-check, nightly benchmark) and
  `retrain.yml` (weekly retrain that opens a PR, never force-pushes).
- **Docs suite** — 14-file, cross-linked documentation under `docs/`, with a docs-vs-code
  consistency gate (`scripts/check_docs_consistency.py`) so docs can never claim something that
  doesn't exist.

### Changed

- Evaluation moved from a leaking random stratified split (PR-AUC 1.000) to a temporal split with
  strictly backward-looking features and time-series CV (honest PR-AUC ≈ 0.98 ± 0.04).
- Rules rewritten vectorized; risk-scoring cached per (data, model) fingerprint; expense
  computation made explicit; narrator routing made specificity-weighted.
- Docker/CI hardened: non-root user, idempotent bootstrap entrypoint, reproducible lockfile.

### Fixed

- Generator crash on tiny day windows (`baf4383`).
- CLI falsy-seed bug (`--seed 0` was treated as unset).
- Duplicate-charge magic-number drift; expense miscount of `CASH_IN`; blend renormalization in
  rule-only mode.

### Docs

- `docs/KNOWN_LIMITATIONS.md` added — every simplification stated plainly, per the project's
  "docs must never claim what doesn't exist" rule.

[0.1.0]: https://github.com/themanoj-025/FinSight/releases/tag/v0.1.0
