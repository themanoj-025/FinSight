#!/usr/bin/env python3
"""Compare a Locust run's CSV stats against the documented SLOs (F.5).

    python scripts/loadtest_check.py --csv loadtest/results_stats.csv

Reads the aggregate ``*_stats.csv`` that ``locust --csv <prefix>`` writes
(responses.csv / failures.csv / stats.csv / stats_history.csv are all written;
the aggregate is ``<prefix>_stats.csv``) and compares each cached-facts
endpoint's p95 latency and error rate against the targets in
docs/technical/SLOs.md:

    * API p95 latency, cached facts endpoints   < 200 ms
    * error rate                                == 0 % (any failure is a miss)

Exits non-zero on any miss, so it composes with the nightly CI load-test job
and ``make loadtest`` — the SLO doc becomes *measured*, not aspirational.

Column layout of the aggregate CSV (locust writes the percentile columns as
bare percentages, e.g. ``95%``): Type, Name, Request Count, Failure Count,
Median Response Time, Average Response Time, Min, Max, Average Content Size,
Requests/s, Failures/s, 50%, 66%, 75%, 80%, 90%, 95%, 98%, 99%, 99.9%,
99.99%, 100%.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

SLO_P95_MS = 200.0  # docs/technical/SLOs.md — keep in sync
OUT_FILE = "loadtest/results_slo_report.txt"

# Endpoints that must satisfy the p95 SLO (the cached-facts surface named in
# SLOs.md). The locustfile names its tasks with these short labels (e.g.
# "monthly-summary"), so the aggregate CSV's Name column holds exactly these
# values; the "Aggregated" row is reported but not gated.
GATED = {
    "health",
    "meta",
    "monthly-summary",
    "category-breakdown",
    "budget-status",
    "recurring-payments",
    "spend-spikes",
    "financial-health",
    "forecast",
    "tips",
    "similar-transactions",
    "risk-scored",
}

# Each gated endpoint MUST have been exercised (count > 0) — a gate that
# silently skips an endpoint is a vacuous pass.
MISSING: set[str] = set(GATED)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="Path to <prefix>_stats.csv from locust")
    args = parser.parse_args()

    with open(args.csv, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("::error::no rows in locust stats CSV — did the load test run?", file=sys.stderr)
        return 1

    lines: list[str] = []
    lines.append("FinSight Agent — load-test SLO comparison (F.5)")
    lines.append(f"  SLO: p95 < {SLO_P95_MS:.0f} ms on cached-facts endpoints, 0% errors")
    lines.append("")

    misses: list[str] = []
    aggregate: tuple[str, str] | None = None
    for row in rows:
        name = row.get("Name", "").strip()
        try:
            count = int(float(row.get("Request Count") or 0))
        except (TypeError, ValueError):
            count = 0
        failures = int(float(row.get("Failure Count") or 0))
        p95 = row.get("95%") or ""
        try:
            p95_ms = float(p95)
        except (TypeError, ValueError):
            p95_ms = float("nan")

        # The CSV has a separate Type column; normalize the Name (locust adds
        # a "GET " prefix when a task doesn't set a custom name — ours does,
        # but accept both forms).
        name = name.removeprefix("GET ").strip()
        if name == "Aggregated":
            aggregate = (p95, str(failures))
            continue
        if name not in GATED:
            continue
        MISSING.discard(name)  # exercised — no longer missing

        err_rate = failures / count * 100 if count else 0.0
        ok = count > 0 and p95_ms < SLO_P95_MS and failures == 0
        status = "PASS" if ok else "FAIL"
        if not ok:
            misses.append(name)
        lines.append(
            f"  {status}  {name:<45} n={count:<6} p95={p95_ms:>8.1f}ms errors={err_rate:>6.2f}%"
        )

    lines.append("")
    if aggregate:
        lines.append(f"  Aggregated: p95={aggregate[0]}ms failures={aggregate[1]}")
    if MISSING:
        lines.append(f"  NOTE: never exercised: {', '.join(sorted(MISSING))}")
        misses += [f"{m} (no requests)" for m in sorted(MISSING)]
    lines.append("")

    ok_all = not misses
    lines.append(
        "RESULT: "
        + (
            "PASS — all cached-facts endpoints meet the SLOs"
            if ok_all
            else f"FAIL — SLO miss on: {', '.join(misses)}"
        )
    )
    print("\n".join(lines))

    out = Path(OUT_FILE)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  (report written to {out})")

    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
