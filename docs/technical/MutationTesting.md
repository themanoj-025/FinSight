# MutationTesting — FinSight Agent: Mutation-Kill Evidence (D.4)

| Field | Value |
| --- | --- |
| Version | v0.1 |
| Last Updated | 2026-08-09 |
| Owner | ML Engineer |
| Status | Active (re-measured on the nightly/on-demand CI job) |

> **What this is.** Coverage percentage tells you how many lines tests *touch*;
> mutation testing tells you how many of those touches are *meaningful*. A
> mutant is a tiny, deliberate fault injected into the source (e.g. `>=` → `>`,
> `+` → `-`, a `not` dropped, a return value altered). If the test suite fails
> on the mutant, the mutation is **killed** — the tests actually pin that
> behavior. If the tests still pass, the mutation **survives**, meaning that
> specific behavior is not asserted anywhere.

## 1. Scope (why these two modules)

Mutation testing is expensive (one test-suite run per mutant), so it is scoped
to the two modules where correctness matters most — the exact fraud-detection
logic a wrong character would silently corrupt:

- `finance_agent/rules.py` — the audit-rule detectors (duplicate charge,
  balance drain, etc.) that must flag fraud without false positives.
- `finance_agent/features.py` — the backward-looking feature matrix every
  model and the retrieval index are built from; a silent feature bug would
  poison model training *and* similar-transaction retrieval at once.

## 2. How to run

```bash
make mutate
```

Runs `mutmut` with a runner scoped to `tests/test_rules.py` +
`tests/test_features.py` (the tests that exercise both modules), writes the
cache to `mutmut-cache/` (gitignored), and prints the per-file summary. The
same command runs in the `mutation` CI job on the nightly schedule / on
demand (`workflow_dispatch`).

**Why not on every push:** one pytest run per mutant, ~5-8 minutes per run.
Mutation testing is a review-grade signal, not a per-commit gate — it is run
on a cadence, and its score is tracked here rather than gating CI latency.

## 3. Kill-score formula and CI regression floor

Kill score = `killed / (killed + survived + timeout)`. Timeout counts as **not
killed** (the tests didn't fail — the run hung). Suspicious / skipped /
no-tests mutants are reported per file but excluded from the denominator:
they aren't meaningful kill outcomes (broken injection / pragma-skipped
branches).

The `mutation` CI job enforces a **55% regression floor** on the total score
(also hardcoded in `.github/workflows/ci.yml` — keep the two in sync). The
floor is set ~8 points below the first green measurement so it gates
*regressions*, not aspirations: the goal is that the score never drops below
the first measured run, not that it hits an unearned target.

## 4. Measured score

| Module | Mutants | Killed | Survived | Kill % | Date |
| --- | --- | --- | --- | --- | --- |
| finance_agent/rules.py | 874 | 558 | 316 | 63.8% | 2026-08-10 (`a5b781e`) |
| finance_agent/features.py | 427 | 261 | 166 | 61.1% | 2026-08-10 (`a5b781e`) |
| **Total** | 1301 | 819 | 482 | **63.0%** | 2026-08-10 (`a5b781e`) |

> First green measurement, from the `mutation` job (workflow dispatch, commit
> `a5b781e`, mutmut 3.7.0). The 55% regression floor is ~8 points below this
> baseline — it gates *regressions*, not the aspirational ≥ 80% target.
> Earlier CI runs measured 0% because `mutmut results --all` (flag syntax
> mutmut 3.7 rejects — `--all` is a *value* option) errored into an empty
> results file; the job now uses `--all true` and parses every status.

## 5. Triage of surviving mutants (updated per measurement)

| Mutant | Why it survives | Verdict |
| --- | --- | --- |
| (first measurement: 482 survivors across `rules.py` / `features.py` — triage in progress; the `mutmut results` cache is on the CI runner, re-run `make mutate` to list them) | | |

**Target:** ≥ 80% killed. Surviving mutants are triaged on every measurement
run: genuinely redundant (behavior pinned elsewhere) vs. a real test gap
(fix the test, don't delete the mutant).

## 6. Related Documents

| Document | Relationship |
| --- | --- |
| [Testing.md](Testing.md) | Test strategy + TC-041..TC-042 |
| [SecurityAndCompliance.md](SecurityAndCompliance.md) | Rules/features correctness is a security control |
| [TechSpec.md](TechSpec.md) | Feature-matrix contract |
