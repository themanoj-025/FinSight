#!/usr/bin/env python3
"""Local SLO check (docs/technical/SLOs.md, Phase D.2).

Samples a running facts API and reports whether it is meeting the published
service-level objectives:

    python scripts/slo_check.py [base_url] [samples]

Defaults: base_url=http://localhost:8000, samples=20. Exits non-zero when an
SLO is missed, so it can be wired into a cron / scheduled GitHub Action as a
poor-man's uptime/status monitor.

Measured SLOs (see docs/technical/SLOs.md):
  * API p95 latency for the cached facts endpoints      < 200 ms
  * /metrics endpoint exposes scrapeable counters        present
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
SAMPLES = int(sys.argv[2]) if len(sys.argv) > 2 else 20
SLO_P95_MS = 200.0
SLO_P95_NAME = "API p95 latency (cached facts endpoints)"


def _get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=5) as resp:  # noqa: S310 — local service
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    print(f"FinSight Agent — SLO check against {BASE}\n")
    failed: list[str] = []

    # health endpoint must be live and load facts
    try:
        health = _get("/api/v1/health")
        ok = health.get("status") == "ok"
        print(
            f"  {'PASS' if ok else 'FAIL'}  /api/v1/health live (rows={health.get('rows')}, "
            f"mode={'rule_only' if health.get('rule_only') else 'blended'})"
        )
        if not ok:
            failed.append("health endpoint not ok")
    except (OSError, RuntimeError) as exc:  # noqa: BLE001 — network/parse failure is a FAIL
        print(f"  FAIL  /api/v1/health unreachable: {exc}")
        print("\nStart the API first: make api")
        return 1

    # p95 latency of a cached facts endpoint
    latencies: list[float] = []
    for _ in range(max(1, SAMPLES)):
        t0 = time.perf_counter()
        _get("/api/v1/health")
        latencies.append((time.perf_counter() - t0) * 1000.0)
    p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]
    ok = p95 < SLO_P95_MS
    print(
        f"  {'PASS' if ok else 'FAIL'}  {SLO_P95_NAME}: {p95:.1f} ms "
        f"(SLO < {SLO_P95_MS:.0f} ms, n={len(latencies)})"
    )
    if not ok:
        failed.append(f"{SLO_P95_NAME} ({p95:.1f} ms)")

    # /metrics must be scrapeable
    try:
        with urllib.request.urlopen(BASE + "/metrics", timeout=5) as resp:  # noqa: S310
            body = resp.read().decode("utf-8")
        ok = "finsight_http_requests_total" in body and "finsight_uptime_seconds" in body
        print(f"  {'PASS' if ok else 'FAIL'}  /metrics exposes scrapeable counters")
        if not ok:
            failed.append("/metrics missing expected series")
    except (OSError, RuntimeError) as exc:  # noqa: BLE001 — /metrics network/parse failure
        print(f"  FAIL  /metrics unreachable: {exc}")
        failed.append("/metrics unreachable")

    print()
    if failed:
        print(f"{len(failed)} SLO miss(es): {', '.join(failed)}")
        return 1
    print("All SLOs met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
