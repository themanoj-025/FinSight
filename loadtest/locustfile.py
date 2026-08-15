"""Locust load test for FinSight Agent's cached-facts API (Phase F.5).

Targets exactly the endpoints the SLOs in docs/technical/SLOs.md measure
(p95 < 200 ms on cached facts). Deliberately excludes ``POST /api/v1/reload``
— it drops the server's facts snapshot (state mutation), not a read path, and
a load test must not repeatedly force it.

Run against a local API:

    make api                  # terminal 1 (or the CI job boots it itself)
    make loadtest             # terminal 2: ramp to 50 users over ~30s, hold, then
                              # compare against the SLOs (scripts/loadtest_check.py)

Or against the live deployment once §3b exists:

    locust -f loadtest/locustfile.py --host https://<your-app>.fly.dev \
        --headless -u 50 -r 2 --run-time 120s --csv loadtest/results \
        --html loadtest/report.html
    python scripts/loadtest_check.py --csv loadtest/results_stats.csv

    # If the API is gated by FINSIGHT_API_KEY, pass it through:
    #   --headers "X-API-Key: <shared-secret>"

The 50-user / 30s-ramp / ~90s-hold profile is a modest, realistic demo-tier
load, not a saturation test — it exists to make the SLOs *measured*, not to
prove maximum throughput.
"""

from locust import HttpUser, between, task

# Cached-facts read endpoints — the SLO surface (docs/technical/SLOs.md).
# NOTE: /api/v1/reload is intentionally absent (destructive, state-mutating).
FACTS_ENDPOINTS = [
    "/api/v1/health",
    "/api/v1/meta",
    "/api/v1/monthly-summary",
    "/api/v1/category-breakdown",
    "/api/v1/budget-status",
    "/api/v1/recurring-payments",
    "/api/v1/spend-spikes",
    "/api/v1/financial-health",
    "/api/v1/forecast",
    "/api/v1/tips",
    "/api/v1/similar-transactions",
    "/api/v1/risk-scored",
]

# Read-heavy (transactions is the largest payload; monthly-summary the most
# commonly rendered KPI) but every endpoint above is exercised.
WEIGHTS = {
    "/api/v1/health": 2,
    "/api/v1/meta": 1,
    "/api/v1/monthly-summary": 4,
    "/api/v1/category-breakdown": 3,
    "/api/v1/budget-status": 2,
    "/api/v1/recurring-payments": 2,
    "/api/v1/spend-spikes": 1,
    "/api/v1/financial-health": 2,
    "/api/v1/forecast": 2,
    "/api/v1/tips": 2,
    "/api/v1/similar-transactions": 2,
    "/api/v1/risk-scored": 2,
}


class FactsUser(HttpUser):
    """Simulates a dashboard user polling the cached facts endpoints."""

    # Human-ish think time between requests (100-400 ms) so the test measures
    # realistic client pacing, not a tight loop.
    wait_time = between(0.1, 0.4)

    @task(4)
    def monthly_summary(self) -> None:
        self.client.get("/api/v1/monthly-summary", name="monthly-summary")

    @task(3)
    def category_breakdown(self) -> None:
        self.client.get("/api/v1/category-breakdown", name="category-breakdown")

    @task(2)
    def budget_status(self) -> None:
        self.client.get("/api/v1/budget-status", name="budget-status")

    @task(2)
    def recurring_payments(self) -> None:
        self.client.get("/api/v1/recurring-payments", name="recurring-payments")

    @task(2)
    def financial_health(self) -> None:
        self.client.get("/api/v1/financial-health", name="financial-health")

    @task(2)
    def forecast(self) -> None:
        self.client.get("/api/v1/forecast", name="forecast")

    @task(2)
    def tips(self) -> None:
        self.client.get("/api/v1/tips", name="tips")

    @task(2)
    def similar_transactions(self) -> None:
        self.client.get("/api/v1/similar-transactions", name="similar-transactions")

    @task(2)
    def risk_scored(self) -> None:
        self.client.get(
            "/api/v1/risk-scored?limit=10&include_explanations=true",
            name="risk-scored",
        )

    @task(2)
    def health(self) -> None:
        self.client.get("/api/v1/health", name="health")

    @task(1)
    def meta(self) -> None:
        self.client.get("/api/v1/meta", name="meta")

    @task(1)
    def spend_spikes(self) -> None:
        self.client.get("/api/v1/spend-spikes", name="spend-spikes")
