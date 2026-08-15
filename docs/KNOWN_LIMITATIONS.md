# Known Limitations — FinSight Agent

This file exists so a technical reviewer never has to *discover* the gaps: every
simplification is stated plainly here. If a limitation you'd expect is missing,
that's a documentation bug — please open an issue.

---

## 1. Synthetic data, not real transactions

Everything runs on a deterministically generated synthetic ledger
(`generate_data.py`, three tiers — tiny / demo / bench, see
[docs/DataGeneration.md](DataGeneration.md)). There is no real PII, no real
bank feed, and the numbers have no connection to anyone's actual finances.
Fraud is **injected and labeled**, so the detection task is easier than
reality. The fraud library is deliberately difficulty-graded: the easy tier
(balance drain, duplicate charge, spend spike) is rule-detectable by
construction, while the hard/adversarial tier (mimicry, account takeover,
seasonal mimicry) is designed to be missed by simple rules — perfect recall on
those is *not* expected (see §16). The realized fraud rate is a construction
choice that lands in a defensible band (the bench tier realizes ~0.06%, in
real-world-adjacent territory; tiny/demo realize higher rates so small samples
still contain positives — `test_fraud_rate_lands_in_a_defensible_band`
enforces 0.05%–5% across tiers).

## 2. Original evaluation leakage — and how it was fixed

Early versions of `model_bench/train_and_compare.py` computed velocity and
category-mean features over the **entire dataset** before a **random** stratified
split. Test rows could therefore reference information from training rows and from
*future* rows of the same account — producing a PR-AUC of 1.000 that would not
survive scrutiny.

The fix (Phase 0 of the remediation, committed as `fix:`):
1. **Temporal split** — sort by `step`, first 80% of steps = train, last 20% = test, no shuffle.
2. **Strictly backward-looking features** — `build_features()` only ever uses
   information at or before each row's own `step` (trailing per-category means
   replaced full-frame means). Enforced by `test_no_temporal_leakage`.
3. **Time-series cross-validation** — selection uses `TimeSeriesSplit(k=5)`;
   `best_model_metadata.json` reports `pr_auc_mean`/`pr_auc_std`, not a point estimate.

The honest headline metric is the CV mean ± std in
`best_model_metadata.json` — currently **0.828 ± 0.055 PR-AUC for LightGBM on
the committed bench-tier ledger** (10.7M rows; 600k-row stratified training
sample, full 2.7M-row temporal holdout; see the README model table). The
holdout PR-AUC is 0.728 with recall 0.86 at the 0.5 threshold — a real,
non-trivial number on a rare-positive class (0.06% fraud), and the
**per-archetype recall table** is the honest view: easy/medium archetypes are
caught at 0.74–1.00 while the adversarial tier (mimicry 0.21, seasonal
mimicry 0.48) is deliberately imperfect. Because the injected fraud is
hand-designed (see §16), the absolute numbers remain illustrative rather than
production-grade.

## 3. Auth is demo-grade — by deliberate, permanent decision

The app can be gated by a shared password (`APP_PASSWORD` env var,
`app/common.py::require_auth()`). This stops casual access to a demo deployment —
it is **not** enterprise auth: no accounts, no OAuth, no RBAC, and no per-user
data isolation. **This is a permanent design choice, not an open gap**: the
project is a synthetic-data portfolio demo with no real multi-tenant need, so
per-user accounts would add real security surface (password hashing, reset
flows, session invalidation) with no product benefit. See the "Deliberate
decision" note in `docs/technical/SecurityAndCompliance.md` §2; if the
codebase is ever repurposed into a real multi-user product, real auth gets its
own dedicated spec. A per-session brute-force cooldown exists (5 failed attempts in
15 minutes locks that browser session for 5 minutes), but there is **no
IP-level throttling** on login — that belongs at a reverse proxy (a template
is provided at `deploy/`, adopt or adapt — see `DEPLOY.md` §9). When
`APP_PASSWORD` is unset the app runs in an openly visible "DEMO MODE — NOT
SECURED" banner state.

## 4. Persistence: SQLite + CSV + joblib

The generated CSV stays the source of truth, but the hot read paths are backed
by an optional SQLite store (`data/transactions.db`, `finance_agent/storage.py`,
enabled via `data.store_path` in config.yaml): a `transactions` table synced
from the CSV, a **materialized** `risk_scores` table — rules + features + model
inference run once per (data, model) fingerprint, so the interactive risk scan
is a SQL point query instead of re-filtering a full in-memory scored frame on
every rerun — and hand-rolled migrations (`PRAGMA user_version`). Residual
limits:

- **Scaling ceiling:** the rules and feature-engineering layers still operate on
  a full in-memory pandas frame (`FinanceFacts.df`), so interactive use is
  comfortable up to roughly 100k–1M rows depending on the machine. Beyond that,
  the vectorized rule detectors and features would need to move into SQL;
  DuckDB is the natural upgrade if the ledger ever outgrows SQLite's
  single-writer model.
- **Single-writer SQLite:** one writer at a time — fine for this app and the
  read-only API, which share the store.
- **`joblib.load` risk:** model bundles are loaded via pickle. Since C.2.4 every
  bundle the training pipeline writes is **HMAC-SHA256 signed**
  (`<bundle>.sig`, key = `FINSIGHT_BUNDLE_KEY` env var or a documented demo
  default), and `tools.py` verifies the signature **before** `joblib.load`: a
  tampered or swapped bundle is refused and the app degrades to rule-only.
  When `FINSIGHT_BUNDLE_KEY` is set, unsigned bundles are refused too — and,
  since the audit remediation, a bundle whose signature doesn't verify against
  the **configured** key aborts the API at startup with a
  `BundleSignatureError` naming the fix (re-sign on the deployment target with
  the real key), instead of silently serving rule-only scores that would mask
  the misconfiguration. Only load bundles produced by this project's own
  training pipeline; never download a `risk_model_bundle.joblib` from an
  untrusted location. The demo default key is **local-development only** — it
  is public (it ships in this repo), so it stops accidental corruption or
  casual tampering, not an adversary with repo access; real deployments must
  set a unique generated `FINSIGHT_BUNDLE_KEY`.

## 5. Multi-user, but demo-scoped

Since the v0.2 stretch work, the generator can produce **multiple focal users**
(`data.focal_users` in config.yaml, e.g. `U_Alex` + `U_Maria`), each with its
own full, internally-consistent ledger (salary, rent, subscriptions, anomalies).
The app sidebar lets you switch which focal user the dashboard, chat, and
reports view; the API accepts `?user=U_X` on per-user endpoints. Scope limits:

- **Switching, not multi-tenancy.** One active focal user at a time, selected in
the sidebar; there are no separate logins, roles, or per-user isolation (the
shared `APP_PASSWORD` gate protects the whole demo, not individual accounts).
- **All users share one fraud model.** The supervised model + rules are trained
to detect anomalies per account; switching users changes the *view*, not the
scoring.
- **Fixed set.** `focal_users` is a generation-time config list; you cannot add a
user from the UI (regenerate data with a new list instead).
- Background accounts still exist only to make the fraud task realistic.

## 6. No real-time ingestion

Transactions are generated in batches, not streamed. The "Live risk scan" scores
the static ledger; there is no webhook/API ingestion path. Outbound alerting
exists, though: with `features.webhook_alerts` + `alerts.webhook_url`
configured, a scan that flags a transaction above the threshold POSTs a small
JSON payload to your endpoint (`finance_agent/alerts.py`) — push out, not
ingestion in.

There **is** a read-only HTTP API (`finance_agent/api.py`, `make api`, OpenAPI
at `/docs`) that exposes the facts layer over `/api/v1`; the Streamlit app
becomes its client when `FINSIGHT_API_URL` is set (docker compose wires this
up). It serves a **startup snapshot** of the ledger + model bundle — see
section 11 for its scope limits.

## 7. Weekly digest is a scheduled convenience, not a pipeline

`finance_agent/digest.py` (`make digest`, or the `digest.yml` scheduled
workflow) builds a Markdown summary of the trailing 7 days of the **currently
generated ledger** and delivers it via Slack Incoming Webhook and/or SMTP email
— both **opt-in** and stdlib-only (`urllib`/`smtplib`). Limits:

- **File-only by default.** With no `DIGEST_SLACK_WEBHOOK` / `digest.email`
  configured it writes `reports/weekly_digest.md` and says so — nothing is sent
  anywhere.
- **Plain text only.** Slack receives a text payload; email is a plain-text
  message. No attachments, no HTML, no charts.
- **Not a real ingestion/alerting system.** It summarizes whatever the latest
  generated dataset contains; there is no event stream, no unsubscribe
  management, and delivery failures raise loudly in the scheduled job (which
  is intentional — silent drops would be worse).
- **One digest per run**, scoped to the default focal user (`data.focal_user`).
- **Window semantics.** The glance line covers the trailing 7 days of the
  ledger; when that week contains no income but a salary deposit exists within
  the trailing ~35 days, the window extends back to the most recent payday (so
  monthly-salary users never see a misleading "income: $0"). The exact window
  is printed in the digest header, so the figures are always scoped to what is
  shown — but the health/tips/budget/recurring sections are month-scoped from
  the facts tools, by design.

## 8. LLM agent: costs and limits

- The LLM only answers from tool outputs, but a bad key or network failure falls
  back to the deterministic narrator.
- Per-session budgets exist (`agent.max_session_turns`, `agent.max_session_tokens`)
  and the app shows remaining budget. Since C.1.2 the **cap is enforced
  server-side**: turn counts and the exact input/output tokens the API reports
  are persisted to `agent.budget_store` (`data/session_usage.db`, keyed by a
  session id carried in the URL), so a page reload cannot silently reset the
  budget. The offline narrator has no budget by design.
- Chat history sent to the API is capped at `agent.max_history_turns` (10) turns;
  older turns are dropped, not summarized.

## 9. Metrics honesty

- CV mean ± std is reported, but the folds are on one synthetic dataset with one
  seed — treat the numbers as illustrative, not as a claim about real-world
  fraud detection performance.
- The rule detectors are heuristic (window/amount thresholds in `config.yaml`);
  they will both miss and over-flag real-world patterns they were not tuned on.

## 10. SHAP explanations are model-dependent

The Fraud & Anomaly Detection page can explain *why* a transaction was flagged
via native LightGBM TreeSHAP (`pred_contrib`) — no `shap` package needed, and
contributions + bias exactly equal the model's log-odds. Scope limits:

- Only available when a trained model bundle is present (rule-only mode has
  nothing to explain).
- Only for tree models the bundle may carry (LightGBM today). If a future
  benchmark picks a non-tree winner, explanations degrade to absent rather
  than approximate.
- They explain the **supervised model's** score, not the blended risk score —
  the rule signal is reported separately in the `reason` column.

## 11. Docs-vs-code discipline

The project's rule: docs must never claim a test, schema, or security control
that doesn't exist. `docs/technical/Schema.md`, `Testing.md`, and
`SecurityAndCompliance.md` were rewritten to describe only what the code actually
does. If you find a drift, that's a bug — please report it.

## 12. Branded PDF export — scope limits

The PDF export (`finance_agent/pdf_export.py`) is a hand-rolled, stdlib-only
writer that produces valid A4 PDFs with the project's brand palette (navy
header, accent rules, styled tables, page footer). Scope limits:

- **WinAnsi text only.** Built-in PDF fonts use `WinAnsiEncoding` (≈ cp1252).
  The report's `± · —` are safe; emoji are mapped to text markers (`[over]`,
  `[ok]`, etc.) and any other non-WinAnsi character becomes `?`.
- **Simple layout engine.** A flowing block model (headings, paragraphs,
  bullets, tables, rules) with greedy word-wrap. No arbitrary nesting, images,
  charts, or vector graphics.
- **No hyperlinks or TOC.** The PDF is a visual rendering of the Markdown
  report — internal links and table of contents are not preserved.
- **Deterministic.** Same markdown in → identical bytes out (the generation
  timestamp comes from the report itself, not from `datetime.now`).

## 13. Cost/observability dashboard — scope limits

The Settings page tracks per-session agent usage (input/output tokens, latency,
estimated cost) in `st.session_state`. Scope limits:

- **Estimates, not exact.** Token counts are taken from the Anthropic API's
  `usage` payload when available (LLM calls); narrator turns use an estimate
  (`len(text) / 4`). Cost is computed from `agent.pricing` in `config.yaml` —
  verify the rates match your actual billing.
- **Session-scoped.** The Settings usage dashboard reflects the current
  session only; the budget-enforcement store (`data/session_usage.db`) does
  persist exact token totals per session id, but there is no historical cost
  dashboard or per-user aggregation.
- **No hard block.** The budget cap (`agent.max_session_turns`,
  `agent.max_session_tokens`) stops the LLM from being called but does not
  affect the offline narrator or tool-only paths.

## 14. Label noise & discovery lag are simplified

The ~2% discovery-lag rate (a fraud label only becomes *knowable* 24h–30d after
the transaction, `label_reported_at_step > step`) and the otherwise clean,
hand-assigned labels are a **simplified model** of real chargeback timelines:
real label noise includes false positives from fraud analysts, merchant
disputes that get reversed, and variable bank reporting windows — none of which
are simulated. The lag exists so streaming-evaluation questions can be asked,
not because it mimics any specific bank's process.

## 15. Merchants and regions are synthetic

Merchant names, categories, and the ~36 regions are combinatorially generated
synthetic placeholders — they are **not** drawn from real geographic or
business distributions, and region coordinates are real-city-ish placeholders
(Portland appears twice). Zipfian popularity and great-circle distances are
realistic *shapes*, not real data. Anything that looks like a real business or
city is coincidence.

## 16. Adversarial fraud patterns are hand-designed

Patterns 9–12 (mimicry, account takeover, bust-out, seasonal mimicry) are
hand-authored to be hard, but they are still **not generated by a real
adversarial process**. A real adversary adapts to the detector; these patterns
are static fixtures with fixed signatures. Per-archetype recall is reported
precisely because imperfect recall on this tier is expected and instructive —
but the numbers should be read as "how hard was this hand-crafted archetype",
not "how well would this model resist an adaptive attacker".

## 17. Bench-tier scale & memory

The `bench` tier is 10.7M rows (20,000 background accounts × 4 years, 200
focal personas) and generates in **~5 minutes** on a mid-range laptop via the
vectorized generator (`python generate_data.py --tier bench`, wall-clock
299s, 10,700,142 rows, 0.06% fraud rate, 288 MB Parquet). A full
six-model 5-fold time-series CV training run takes **~9.5 minutes**
(`model_bench/train_and_compare.py --data data/transactions.parquet`,
562.7s) and refreshes `best_model_metadata.json` + `model_bench/results/`.
Measured 2026-08-08 on a 16 GB machine; the nightly `benchmark-nightly` job
enforces them as **wall-clock regression gates** when dispatched with
`tier=bench` — generation ≤ 900 s and training ≤ 1800 s, both ~3× the
measured baselines so a return of the old O(n²)-scale generator bugs fails
the job loudly (mirroring the demo-tier 60 s gate in `data-realism`).

**Sampled training, by design.** Because the temporal train window holds ~8M
rows, the benchmark downsamples it to `model_bench.max_train_rows` (600k,
stratified by fraud class — every fraud positive is kept) so the 6-model CV
stays practical; the **temporal test window is never sampled** (2.7M rows).
The metadata records `max_train_rows` in `dataset`/`config`, so the
evaluation is always on the full holdout. The sample breaks per-account
feature-history contiguity (rolling-window features see a sparser history),
which the leakage-safe proxies in §18 tolerate but a production system would
need persistent history tables for.

**Memory-lean Parquet loading.** The benchmark reads Parquet with
`dtype_backend="pyarrow"` (string columns stay arrow-backed instead of
materializing one Python `str` per cell — the object-dtype frame would need
~10 GB for 10.7M rows) and converts numeric columns back to native numpy
dtypes. Numeric/boolean columns are cheap to convert; string columns stay
compact. This keeps a full bench load at ~3.5 GB.

`bench` output is Parquet and is never loaded directly by the Streamlit app
(demo-tier rollups only); analysis over the Parquet is expected to use
columnar/point queries (e.g. via DuckDB or pandas with column selection).

## 18. Feature approximations

The causal features mirror the generator's signals but are approximations:
`is_out_of_home_region` stands in for the spec's "first-time region" (it
flags *any* away-from-home transaction, not only never-seen regions), and
`is_new_payee` / `is_new_merchant` are computed as first-seen-in-ledger flags.
These are deliberately simple, leakage-safe proxies — a real deployment would
want persistent per-account history tables.

## 20. MLflow planned, not implemented (Optuna HPO *is* implemented)

**MLflow** (experiment tracking) is **not** implemented — no code imports it
and no dependency ships it (`config.yaml features.mlflow_tracking: false`).
This is a deliberate decision, not an omission: experiment tracking beyond
the git-tracked `best_model_metadata.json` snapshot is intentionally not
built — the flag exists as a documented extension point, not a claim of
current capability. If it's ever built, the roadmap is a local tracking store
(`mlruns/`, gitignored) that every `train_and_compare.py` run logs to —
without replacing the tracked metadata JSON, which stays the canonical,
git-visible "what's deployed" snapshot.

Related: **Optuna hyperparameter optimization is implemented**, not just
claimed — `make hpo` (`model_bench/hpo.py`, the `hpo` pip extra) runs an
Optuna study over the LightGBM family's key hyperparameters
(`num_leaves`/`learning_rate`/`min_child_samples`/`reg_alpha`/`reg_lambda`/
`feature_fraction`) using the same mean-PR-AUC over 5-fold `TimeSeriesSplit`
as the benchmark, persisted to `model_bench/results/hpo_study.db`
(optuna-dashboard compatible), with a parameter-importance chart at
`model_bench/results/hpo_param_importance.png`. Scope limits: adoption is a
documented human review (`make hpo-promote`) gated by
`model_bench.hpo.min_improvement` (0.01 PR-AUC) to avoid chasing CV noise —
`make train` uses the tuned params only after that, and the provenance
(study id, tuned params, improvement) lands in `best_model_metadata.json`
(`hpo_study_id`); an exploratory `make hpo` alone changes nothing. Both flags
live in `config.yaml features.*`.

Related: **similar-transaction retrieval is implemented**, not just claimed —
`finance_agent/retrieval.py` column-standardizes the engineered feature
vectors (z-score, then L2-normalize) and answers nearest-neighbor queries with
exact-L2 search, using `faiss-cpu` when installed and an identical numpy
brute-force fallback otherwise (the `retrieval` pip extra). Its
`find_similar_transactions` tool is wired end-to-end: registered in the agent's
`TOOL_SPECS` (with a system-prompt rule), exposed as `GET
/api/v1/similar-transactions`, surfaced on the Fraud page under each flagged
transaction, and covered by `tests/test_retrieval.py` (including a
same-archetype-neighborhood quality assertion). Scope limits: the index is
rebuilt per process from the current ledger, retrieval is feature-space (not
semantic-text) similarity, the standardization uses global ledger column stats
(fine for a display-time similarity tool — it never feeds the model, so there
is no training/evaluation leakage surface), and the whole feature is gated by
`config.yaml features.faiss_retrieval`.

## 21. HTTP API: scope limits

The FastAPI facts API (`finance_agent/api.py`) is a read-only view over the
same CSV/joblib artifacts — it does not change the architecture's fundamental
limits, and it adds a few of its own:

- **Startup snapshot.** The API loads one `FinanceFacts` per process. If you
  regenerate data or retrain while it runs, it keeps serving the old ledger
  until `POST /api/v1/reload` or a restart. The app calls reload automatically
  after regeneration; a manual API user must do it themselves.
- **Response caching.** Because of that snapshot semantics, the read-only facts
  endpoints memoize their JSON-safe response per query-params tuple and
  invalidate on `POST /api/v1/reload` — under load every client is served the
  same cached snapshot instead of each request recomputing pandas groupbys /
  TreeSHAP over the ledger. This is what makes the documented SLO (p95 < 200 ms
  under the 50-user load profile, see `docs/technical/SLOs.md`) measurable:
  measured p95 across all endpoints is 17–18 ms with 0% errors.
- **Demo-grade auth.** `FINSIGHT_API_KEY` is a shared secret in an `X-API-Key`
  header — no accounts, no per-user isolation. Same caveat as the app's
  `APP_PASSWORD` gate.
- **Rate limiting is optional and in-process.** Set `FINSIGHT_RATE_LIMIT_PER_MIN`
  for a per-IP sliding-window limit on `/api/*` (429 + `Retry-After`); it is
  **off by default** so local dev / CI / load tests are unaffected, and it is
  per-worker-process state — a horizontally scaled or edge deployment needs a
  shared limiter (e.g. a Redis-backed one at the proxy).
- **Not an ingestion API.** There are no write endpoints for transactions;
  data still comes only from `generate_data.py`.
- **Single instance.** Two API processes would each hold their own snapshot
  (eventually consistent at best). Fine for a demo; not a horizontally scaled
  service.
- **Pagination** exists on the full-ledger `transactions` endpoint
  (`limit`/`offset`, default 500, max 5000, with a `total` count); the
  `risk-scored` endpoint has its own `limit` parameter. The remaining fact
  endpoints (`monthly-summary`, `category-breakdown`, …) return small,
  pre-aggregated payloads by design.

## 22. Deferred roadmap (intentionally not built yet)

For completeness, the things this project consciously does **not** do yet —
each is a small, well-scoped next step if time allows:

- ~~**Mutation testing**~~ **Done (D.4)** — `mutmut` on `rules.py`/`features.py`
  (`make mutate`, dev-only, config-driven in `[tool.mutmut]`); the `mutation` CI
  job runs nightly/on-demand and gates a documented kill-score regression floor
  (see `docs/technical/MutationTesting.md`).
- ~~**Contract fuzzing and load testing**~~ **Done (F.3 / F.5)** —
  `make contract-fuzz` (Schemathesis against the committed `openapi.v1.json`)
  and `make loadtest` (Locust against the cached-facts endpoints, compared with
  the SLOs in `docs/technical/SLOs.md`) run in their own nightly/on-demand CI
  jobs; both exclude the destructive `/reload` endpoint by design.
- ~~**Browser-level accessibility automation**~~ **Done (F.4)** —
  `make a11y` + the weekly `accessibility` workflow run Playwright + axe-core
  over all 8 pages (zero critical/serious violations) plus a 375px
  mobile-overflow check, against a locally-launched Streamlit instance.
- **Live hosted deployment** — no hosted URL is published; deployment is
  documented (`DEPLOY.md` runbook + `docs/technical/Deployment.md`) and the
  `fly.toml`/Dockerfile/entrypoint are deploy-grade, but not yet operated.
  When deployed, the SLOs in `docs/technical/SLOs.md` (with
  `scripts/slo_check.py` + the `status-page` workflow's `STATUS_URL` variable)
  become the operating target.
- ~~**mypy on `app/` + `tests/`**~~ **Resolved** — the typecheck gate now
  covers the whole project (`finance_agent/`, `model_bench/`, `app/`,
  `tests/`); the 17 pre-existing errors in dynamic/kwargs-heavy test code
  were fixed (typed fixture dicts, streamlit stub unions for
  `date_input`/`radio`/`write_stream`, and a properly-typed uvicorn test
  fixture). Coverage is gated at 75%+ (measured 82%).
- ~~**Structured JSON request logging / Sentry**~~ **Done (D.1)** —
  `finance_agent/observability.py` installs a JSON formatter + correlation-id
  filter on the root logger (every log line is structured JSON carrying the
  request/session id), the API threads/echoes `X-Request-Id`, and Sentry
  reporting is wired but inert without a `SENTRY_DSN` env var (asserted by
  `tests/test_observability.py`). `/metrics` still covers the scrapeable
  surface.
- ~~**Webhooks for live risk alerts**~~ **Done** — `finance_agent/alerts.py`
  (opt-in: `features.webhook_alerts` + `alerts.webhook_url`, or the
  `FINSIGHT_WEBHOOK_URL` env var) POSTs a small JSON payload when a live risk
  scan flags transactions above the threshold; deduplicated per transaction
  (`alerts.state_path`) and failure-tolerant (a dead endpoint logs and never
  breaks the scan). It is **outbound alerting only** — there is still no
  inbound event stream/ingestion path (see §6).
- **Experiment tracking (MLflow)** — see §20 above (Optuna HPO is done).
- **i18n / multi-currency** — explicitly deferred, not forgotten: the ledger,
  currency formatting, and UI are English/USD-only. This is a consciously
  opted-out polish item rather than an accidental gap — it is deliberately
  parked until after the live deployment and the demo video exist, since both
  have far higher return per hour invested.
