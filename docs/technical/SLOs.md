# Service-Level Objectives (Phase D.2)

Evidence of *operating a service to a target*, not just building one. These
are the written targets for the parts of FinSight Agent that have a
"service" framing; the local checker in
[`scripts/slo_check.py`](../../scripts/slo_check.py) measures them against a
running instance and fails loudly on a miss, so the targets are checkable, not
aspirational.

## Objectives

| Objective | Target | How it is measured | Scope |
| --- | --- | --- | --- |
| API p95 latency, cached facts endpoints | **< 200 ms** | `scripts/slo_check.py` samples `/api/v1/health` (n=20); the nightly `load-test` job (F.5) hammers every cached-facts endpoint at 50 users and gates p95 per endpoint via `scripts/loadtest_check.py` | Facts API (`finance_agent/api.py`) |
| Agent time to first token, LLM path | **< 4 s** | Session-usage ledger records per-call latency (`agent.py::SessionUsage`); the Settings page shows it | `FinanceAgent._llm_answer` |
| Agent response time, offline narrator | **< 1 s** | Same ledger — narrator turns are latency-metered at zero cost | `FinanceAgent._narrator` |
| Metrics endpoint availability | **always served** | `/metrics` returns Prometheus text format; `slo_check.py` asserts the series exist | Facts API |
| Health endpoint availability | **always served** | `/api/v1/health` returns `status: ok` whenever data artifacts exist | Facts API |
| Uptime (when deployed) | **99.5% monthly** | Scheduled ping → status log (see below) | hosted instance |

## How to measure locally

```bash
make api                      # terminal 1 — start the facts API
python scripts/slo_check.py   # terminal 2 — measure and report
```

The script exits non-zero on a miss, so it composes with cron, `make`, or a
scheduled CI job.

## Load testing (Phase F.5) — the SLOs, measured

The nightly `load-test` CI job (and `make loadtest`) runs a Locust workload —
50 users ramping at 2/s over 120 s against every cached-facts endpoint
(`/api/v1/health`, `meta`, `monthly-summary`, `category-breakdown`,
`budget-status`, `recurring-payments`, `spend-spikes`, `financial-health`,
`forecast`, `tips`, `similar-transactions`, `risk-scored`; never `/reload`) —
then compares each endpoint's p95 and error rate against the targets above
with a pass/fail readout (`scripts/loadtest_check.py`). Artifacts (CSV,
HTML report, SLO comparison) are uploaded from every run.

Two measurement rules keep the number honest:

- **Warm before timing.** The first request per endpoint pays a one-time build
  cost (scored frame, retrieval index, facts snapshot). The SLO is about the
  *cached* path, so the job warms every endpoint before the timed window — a
  cold-heavy percentile measures startup, not steady state.
- **No vacuous passes.** Every gated endpoint must have been exercised
  (request count > 0) for the run to pass, and the gate matches the locustfile's
  task names — a checker that silently skips endpoints fails the run.

Measured warm-path work (first F.5 run, demo tier) that landed to meet the
200 ms target: month-label computation memoized per `FinanceFacts` instance
(`monthly-summary` 420 ms → 8 ms), the similar-transactions helper no longer
computes per-row TreeSHAP it never reads (1.3 s → ~5 ms), and the risk scan
degrades gracefully (no 500) on a stale-store fingerprint collision.

The finishing piece is **API-level response caching**: the read-only facts
endpoints memoize their JSON-safe result per query-params tuple and invalidate
on `POST /api/v1/reload` (the API serves a startup snapshot, so responses are
deterministic between reloads). Without it, the first F.5 run measured p95 of
1.3–3.7 s across the endpoints with `risk-scored` (SHAP per request) erroring
out entirely under 50 concurrent users; with it, the full 50-user/120 s profile
measures **p95 = 17–18 ms and 0% errors on all 12 endpoints** (~20k requests
served) — the SLO is measured and met, not aspirational.

## Prometheus

`GET /metrics` exposes process-scoped counters in the Prometheus text format
(zero-dependency, no `prometheus-client`):

```
finsight_http_requests_total{route="/api/v1/health"} 42
finsight_http_latency_seconds_total{route="/api/v1/health"} 1.23
finsight_uptime_seconds 3721.0
finsight_info{version="0.1.0"} 1
```

Point any Prometheus at `scrape_configs: [{ job_name: finsight, static_configs: [{ targets: ['<host>:8000'] }] }]`,
or read them directly in a status page. Counters reset on process start — they
are a local observability surface, not a durable history.

## Uptime / status tracking (when deployed)

For a deployed instance, add a scheduled GitHub Action that pings
`/api/v1/health` every 15 minutes and appends one line (timestamp, p95-ok,
http-ok) to a small status log, rendered as a static status page — this
turns the SLOs above into a time series rather than a point-in-time check.
The `slo_check.py` exit code is the ready-made failure signal for that job.

## Agent latency budget

The agent SLOs are met by construction on the demo tier: the narrator path
runs entirely in-process on the pre-scored ledger, and the LLM path streams
text as it arrives (time-to-first-token, not full-response latency). The
per-call `latency_ms` column in the Settings usage dashboard is the honest
record; the `< 4 s` first-token target applies to the LLM path with a warm
Anthropic connection.
