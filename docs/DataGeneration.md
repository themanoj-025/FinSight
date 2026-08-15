# Data Generation — FinSight Agent: Synthetic Ledger Design

| Field | Value |
| --- | --- |
| Version | v0.2 |
| Last Updated | 2026-08-08 |
| Owner | Data Engineer |
| Status | Approved |

This document describes the synthetic data generator end to end: the **three
generation tiers**, the **persona population model**, the **15-pattern fraud /
anomaly library**, seasonality & drift, the multi-account structure, the
geography / merchant taxonomy, and the **reproducibility contract**. It is the
source of truth for *why* the generated data looks the way it does — the column
reference lives in [Schema.md](technical/Schema.md), and the honest caveats
about what is still synthetic live in [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

Entry points: `generate_data.py` (CLI), `finance_agent/datagen.py` (engine),
`finance_agent/personas.py`, `finance_agent/merchants.py`,
`finance_agent/fraud_patterns.py`.

---

## 1. Three tiers, not one dataset

The generator ships **three tiers** selectable via `--tier` (default from
`config.yaml data.tier`), so tests stay fast, the app demo stays usable, and
the model benchmark gets a dataset big enough to be credible:

| Tier | Purpose | Background accounts | Time span | Output | Notes |
| --- | --- | --- | --- | --- | --- |
| `tiny` | unit tests, CI, fast local dev | 20 | configurable (default 90 days) | CSV | matches the legacy footprint; medium/hard fraud rates unscaled |
| `demo` | Streamlit app, README, day-to-day | 2,000 | configurable (default 90 days) | CSV | default for `make data` / `make run`; app loads this synchronously |
| `bench` | `model_bench` evidence, stress tests | 20,000 | **1,460 days (4 years)** | Parquet | medium/hard fraud rates scaled down; never loaded by the app |

Tier table (`finance_agent/datagen.py::TIER_DEFAULTS`):

| Key | tiny | demo | bench |
| --- | --- | --- | --- |
| background accounts | 20 | 2,000 | 20,000 |
| `fraud_scale` (medium/hard rate multiplier) | 1.0 | 1.0 | 0.5 |
| background bust-out fraction | 0.0 | 0.01 | 0.008 |
| window (`days`) | 90 | 90 | 1,460 (4 years) |
| default focal personas | 1 | 1 | 200 |

Background fraud is driven entirely by `bust_fraction` (pattern 11 bust-out);
there is no separate non-bust background fraud injection rate.

The `bench` tier owns its multi-year window and a **200-persona focal
population by default** (`--days` / `--focal-users` still override; the tier
default also **overrides `data.focal_users` from config.yaml**, which is an
app/tiny/demo knob): the huge legitimate background ledger would otherwise
drown the injected fraud and push the fraud rate below the defensible band —
with 200 focal personas × 4 years the realized bench rate lands at ~0.06%.
`tiny` / `demo` keep using `data.focal_users` from config (default `U_Alex`),
and the app's sidebar can switch among however many were generated. Row counts scale with background
accounts × span: the default `bench` run produces **10.7M rows**; a `demo`
ledger over 2 years is roughly 150k–300k rows.

Measured wall-clock (16 GB laptop, 2026-08-08): `demo` generation ~18 s
(63k rows), `bench` generation ~5 min (10.7M rows), bench training ~9.5 min
(see [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) §17).

Rules of thumb:

- **CI only ever runs `tiny`** (`python generate_data.py --tier tiny`). The
  `demo` / `bench` tiers are generated locally or in scheduled/manual jobs
  (`ci.yml benchmark-nightly`, `ci.yml data-realism`, `retrain.yml`), never on
  every push.
- **The demo tier has a nightly wall-clock budget.** The `data-realism` job
  times `generate_data.py --tier demo` and fails if it takes ≥ 60 s — the §1
  acceptance criterion, so a silently-slower generator can never ship. The
  same job runs the realism suite (§10) against demo-tier data.
- **The bench tier has nightly wall-clock regression gates.** When
  `benchmark-nightly` is dispatched with `tier=bench`, it times generation
  (≤ 900 s vs the ~299 s measured baseline) and training (≤ 1800 s vs the
  ~563 s measured baseline) and fails the job on either — a return of the
  O(n²)-scale generator bugs fails loudly instead of silently eating the
  120-minute job timeout. The budgets are regression gates (~3× headroom),
  not tight spec enforcement.
- `tiny` must behave like the legacy generator: existing tests keep passing
  unmodified, and the ledger gains the new additive columns (§8) rather than
  changing the old ones.
- The Streamlit app must never load a `bench`-tier file directly — the
  `bench` ledger is exclusively for `model_bench/train_and_compare.py` and
  ad-hoc analysis.
- All tier outputs are gitignored and regenerated deterministically
  (`data/transactions.csv`, `data/transactions.parquet`).

### CLI

```bash
python generate_data.py                          # demo tier -> data/transactions.csv
python generate_data.py --tier tiny              # fast tests / CI
python generate_data.py --tier bench             # -> data/transactions.parquet
python generate_data.py --days 90 --seed 42
python generate_data.py --tier demo --focal-users U_Alex,U_Maria --n-background-accounts 400
```

`--days`, `--seed`, `--start-date`, `--user` / `--focal-users`,
`--n-background-accounts`, `--format csv|parquet`, `--output`, `--config`,
`--verbose`. Output format is chosen by the output extension; `bench` defaults
to Parquet. `--seed 0` is a valid, respected value.

---

## 2. Persona population model

Each persona is a parameterized archetype that generates its own income /
spending / timing distributions. Parameters are randomized per individual
around the archetype prior (Dirichlet-sampled category weights, income within
archetype bounds, a cost-of-living multiplier), so no two "young
professionals" spend identically.

### Archetypes (`finance_agent/personas.py`)

| Persona | Income pattern | Spending signature | Notes |
| --- | --- | --- | --- |
| `young_professional` | biweekly/semimonthly salary, $55k–$95k | dining/entertainment/subscriptions heavy, moderate rent | frequent small P2P transfers (bill splitting) |
| `dual_income_family` | two staggered salaries, $110k–$180k | groceries/childcare/utilities dominant, larger recurring payments | occasional large one-offs (back-to-school, holidays) |
| `gig_worker` | irregular lumpy CASH_IN (3–7/week) | volatile spend, thin buffer | makes CASH_IN a first-class event; near-zero-balance periods |
| `retiree` | monthly pension-style deposit, $28k–$55k | health-heavy, low velocity | a burst of activity is *more* anomalous than for high-velocity personas |
| `recent_graduate` | entry salary + fixed loan payment | high fixed-obligation ratio, thin buffer | stresses buffer-months at realistic edge values |
| `small_business_owner` | irregular large inflows/outflows | wide variance, higher average amount | legitimate-but-large transactions the model must not false-positive |

`assign_archetypes` guarantees **every archetype appears at least once** when
the persona count ≥ 6 (the app's default user is pinned to
`young_professional`).

### Per-persona parameters (randomized within archetype priors)

Income level, income cadence (`weekly` / `biweekly` / `semimonthly` /
`monthly` / `irregular`), payday anchor day/weekday, rent share, savings rate,
Dirichlet category-weight vector, subscription count & mix, cost-of-living
multiplier (0.85–1.25), spend multiplier, transaction velocity, credit-card
ownership + share of spend, CASH_IN rate/mean, loan payment, home region +
usual travel regions, annual raises (2–5%/yr), opening balance (0.5–3 months
of expenses; thinner for thin-buffer personas).

### Background pool

Background accounts reuse the **same persona system** at reduced detail
(`BackgroundProfile`): sampled archetype with randomized parameters, batched
per account (not object-per-day), no subscriptions/credit structure. A
configurable fraction carries the **bust-out** fraud archetype (`bust_fraction`),
so the `bench` evaluation measures fraud detection on a whole population, not
just the focal user.

---

## 3. Temporal depth, seasonality & drift

`demo` / `bench` tiers support multi-year windows with:

1. **Seasonality** — month-of-year spend multipliers per category
   (`finance_agent/merchants.py::SEASONAL_MULTIPLIER`): holiday shopping bump
   (Nov–Dec ×1.5–1.7), January dining dip (×0.65), back-to-school shopping
   bump (Aug ×1.2), winter/summer utility extremes (×1.2–1.35). The
   holiday window helper (`is_holiday_window`) defines Nov 15 – Dec 31.
2. **Payday-aligned spending bursts** — discretionary transaction rates are
   multiplied by a days-since-payday cluster table
   (`_CLUSTER = {0: 0.55, 1: 2.0, 2: 1.6, 3: 1.35, 4: 1.15, 5: 1.0, 6: 0.95}`)
   so the 3–5 days after each payday carry most of the spend — exactly the
   pattern rolling-window features (`sum_amount_prev7_*`) are built to catch.
3. **Annual raises / income drift** — each persona's per-paycheck income is
   multiplied by `∏(1 + raise)` per year (2–5%/yr, deterministic per persona);
   rent, utilities, and subscriptions inflate at 2.5%/yr. A multi-year model
   therefore has to handle genuine non-stationarity.
4. **Concept drift for fraud** — injection is scoped **per calendar year**
   (one `PatternCtx` per year in the window), and medium/hard rates scale with
   the tier's `fraud_scale`. Per-archetype recall broken out by time bucket is
   what makes "did the model degrade over time" answerable.
5. **Life events** — a low-probability chance per persona of a large
   legitimate one-off (car purchase, medical bill, tuition), produced as a
   **hard negative** (§5 pattern 13) and explicitly never labeled fraud.

---

## 4. Geography & merchant taxonomy

- **Regions** — a fixed table of ~36 synthetic US-style (city, state, lat,
  lon) pairs with ids `R00_portland` … `R35_portlandme`. Great-circle
  distances come from `haversine_miles`, precomputed for every pair at import
  time (`features._REGION_DIST`), so the per-row distance feature is a dict
  lookup.
- **Merchant catalog** — ~300 named synthetic merchants built combinatorially
  from curated name pools per (category, subcategory), deterministic and
  import-only. A **Zipfian popularity weighting** (`s = 0.9`) makes a handful
  of dominant chains carry most of the volume (the shape test in
  `test_merchants.py` asserts the top-10 share stays in a believable band).
- **Category hierarchy** — flat `category` (unchanged, backward compatible) →
  leaf `subcategory` (e.g. `dining > coffee_shops`), plus coarse
  `category_group` (`income` / `fixed` / `discretionary` / `savings` /
  `transfer`).
- **Home vs. away** — every persona has a home region and two usual travel
  regions. ~96% of discretionary transactions are at home; travel patterns
  (§5) and fraud archetypes 6/10/14 exploit away-from-home and first-time
  regions as a signal dimension.

---

## 5. Fraud & anomaly pattern library — 15 difficulty-graded archetypes

All generation lives in `finance_agent/fraud_patterns.py`. Each pattern is a
pure generator function taking a `PatternCtx` (persona + injection window +
history trackers + tier scale) and returns partial rows tagged with a
`fraud_archetype` slug; balances are resolved later by the vectorized balance
pass. Injection is deterministic given the ctx's RNG substream.

Per-persona-per-year injection rates (`PATTERN_RATES`); easy patterns are
deterministic when the window fits (`force_easy`), medium/hard fire with the
given probability and are multiplied by the tier's `fraud_scale`. Each pattern
has a minimum window (`_MIN_DAYS`) below which it cannot play out.

### Easy — rule-detectable (keep the classic three)

| # | Slug | Signature |
| --- | --- | --- |
| 1 | `balance_drain` | transfer sized to ~60% of live balance + rapid cash-out |
| 2 | `duplicate_charge` | same merchant + amount twice in hours (anomaly, not fraud) |
| 3 | `spend_spike` | category spend spike ~4× daily baseline (anomaly, not fraud) |

### Medium — needs the supervised model

| # | Slug | Signature |
| --- | --- | --- |
| 4 | `card_testing` | burst of 3–6 small (<$8) charges then one large charge |
| 5 | `slow_balance_drain` | 5–9 small transfers over 2–3 weeks (rolling-window drain) |
| 6 | `new_payee_transfer` | first-time-region access + large transfer to a never-seen payee |
| 7 | `subscription_creep` | several new small recurring charges inside a week |
| 8 | `refund_abuse` | purchase → refund → repurchase loop, repeated |

### Hard / adversarial — deliberately imperfect recall

| # | Slug | Signature |
| --- | --- | --- |
| 9 | `mimicry` | fraud drawn from the persona's *own* category/amount distribution |
| 10 | `account_takeover` | low-and-slow baseline migration over weeks, then a final drain (the migration rows are unlabeled — catching it early is the model's job) |
| 11 | `bust_out` | background account: long trust-building, then one big drain |
| 12 | `seasonal_mimicry` | fraud sized to blend into the holiday spend bump |

### Hard negatives — NOT fraud, but must resemble it

| # | Slug | Signature |
| --- | --- | --- |
| 13 | `hard_negative_life_event` | legitimate large one-off purchase / medical bill / tuition |
| 14 | `hard_negative_travel` | legitimate first-time-region trip (hotel + dining + transport) |
| 15 | `hard_negative_rapid_burst` | legitimate grocery + gas + coffee in one afternoon (must not look like card testing) |

Hard negatives are the precision story: they carry `isFraud = 0` and
`is_anomaly = 0` with a `fraud_archetype` slug, so per-archetype metrics can
show how often the model false-positives them.

### Label realism

- **Discovery lag** — ~2% of fraud rows carry `label_reported_at_step > step`
  (24h–30d later), mimicking chargeback-reporting delay. The label exists in
  the final snapshot, but a streaming evaluator that only trusts labels
  reported up to time *t* would not see it yet.
- **Class imbalance** — fraud/anomaly rates land in a defensible band
  (enforced by `test_fraud_rate_lands_in_a_defensible_band`, 0.05%–5%): the
  `bench` tier realizes ~0.06% (real-world-adjacent territory), while
  `tiny`/`demo` are higher so small samples still contain positives.
  `datagen.tier_stats` reports the realized rate and per-archetype counts
  after every run.

---

## 6. Multi-account structure per persona

Focal personas get 2–3 linked accounts: `checking` (the persona id), `savings`
(`{id}_Sav`), and optionally `credit` (`{id}_Cred`). Realistic inter-account
flows are first-class rows:

- **Auto-savings transfer** — a fixed % of each paycheck, posted the hour after
  payday (`checking TRANSFER` + `savings CASH_IN` pair).
- **Credit-card autopay** — each month's card spend is paid in full from
  checking (category `credit`, excluded from expense totals in
  `finance_agent/rules.py::expense_rows`).
- **P2P bill-splitting** — small recurring transfers to a named friend.

Credit accounts carry **negative (debt) balances** (clamped at 0 from above),
which the feature layer handles by clamping before `log1p` (§9). Every row is
tagged with `account_type`; the app's Dashboard filters by account channel.

---

## 7. Background population realism

Background accounts (the "not focal" majority of rows) are generated from the
same persona archetype system at reduced detail and carry their own income,
spend rate, category weights, region, and (optionally) the `bust_out` fraud
archetype. At `bench` scale the model's false-positive rate on the legitimate
background population is a reported metric (`cohort_fairness.csv`,
`per_archetype_recall.csv`), making the evaluation a genuine
fraud-detection benchmark rather than an anomaly detector for one persona.

---

## 8. Schema summary

29 columns — the legacy PaySim-style set (unchanged, backward compatible) plus
11 additive v2 columns. Full reference in [Schema.md](technical/Schema.md).

| Column | Type | Notes |
| --- | --- | --- |
| `persona_id` | string | every account (focal and background) has one |
| `persona_archetype` | string (enum) | one of the §2 archetypes |
| `account_type` | string (enum) | `checking` / `savings` / `credit` / `background` |
| `merchant_region` | string | merchant's region (= transaction region) |
| `transaction_region` | string | where the transaction occurred |
| `home_region` | string | the persona's home region |
| `category_group` | string | coarse parent of `category` |
| `subcategory` | string | leaf of the category hierarchy |
| `fraud_archetype` | string, `""` for legit | which of the 15 patterns generated this row |
| `label_reported_at_step` | int | discovery-lag timestamp (≥ `step`) |
| `simulation_year` | int (derived) | convenience column for seasonality/drift analysis |

---

## 9. Generation architecture & reproducibility

### Vectorized hot path

`finance_agent/datagen.py` generates each persona's full transaction stream
with NumPy/pandas array operations over the whole time span:

- payday arithmetic (`_payday_mask`, `_days_since_payday`), Poisson draws per
  category per day, lognormal amounts — **no `iterrows()` / `.apply(axis=1)`**
  anywhere in the hot path;
- balances via a per-account **clamped cumulative sum** (Lindley's recursion,
  vectorized per account) instead of a Python loop; balance-dependent fraud
  amounts (drains) are resolved in a two-pass `_resolve_drains` step;
- the background population is batched per account profile (the per-account
  loop is over profiles, each fully vectorized), so `bench`-scale generation
  stays bounded in memory and well under the time budget.

### Seed contract (`SeedSequence` substreams)

A single top-level `--seed` produces byte-identical output across runs because
every stochastic consumer draws from its **own independent substream**:

```
ss = np.random.SeedSequence(seed)
batch_rng        = default_rng(ss.spawn(1)[0])      # employer picks, shared draws
personas         = sample_personas(users, seed2, batch_rng)   # one substream per persona
bg_profiles      = sample_background(n_bg, seed3, default_rng(ss.spawn(4)[0]))
per-persona rng  = default_rng(ss.spawn(len(personas)*2)[i])
background rng   = default_rng(ss.spawn(len(personas)*2+1)[0])
discovery-lag    = default_rng(ss.spawn(5)[0])
```

A reordering in the code can therefore never change another persona's data —
this is what makes `tests/test_generate_data.py::test_seed_determinism_at_scale`
(and the tiny-tier determinism tests) meaningful.

### Feature safety

`finance_agent/features.py::build_features` is strictly backward-looking and
degrades gracefully: a legacy frame missing the v2 columns still builds, with
the causal features neutral (0.0 / `account_checking = 1`). Credit accounts'
negative balances are clamped to 0 before `log1p`, so no fold ever sees NaN
(enforced by `tests/test_features.py::test_no_nans_with_negative_credit_balances`).

### Storage

`tiny` / `demo` write CSV (`data/transactions.csv`); `bench` writes Parquet
(`data/transactions.parquet`, gitignored). The optional SQLite store
(`finance_agent/storage.py`) mirrors the CSV with schema v2 migrations for the
new columns and materializes risk scores — see
[Schema.md](technical/Schema.md) §7 for the migration list.

---

## 10. Validation & tests

The generator is validated at three levels:

1. **Fast suite** (`-m "not slow"`) — determinism, seed-0, balance continuity,
   feature no-leakage + no-NaN, pattern-library unit tests
   (`tests/test_fraud_patterns.py`, `tests/test_merchants.py`).
2. **Slow realism suite** (`tests/test_data_realism.py`, `-m slow`) — balance
   invariants on a demo-tier ledger, fraud-rate band, archetype coverage,
   seasonality, multi-account structure, region signal, discovery lag,
   income drift over years, life events not being fraud, savings growth.
   Runs in the nightly `data-realism` CI job alongside the demo-tier
   **60 s generation wall-clock gate** (§1).
3. **`model_bench`** — the benchmark reports per-archetype recall, cohort
   fairness, temporal stability, and calibration (see README "Model
   benchmark" and `model_bench/results/`).

A statistically broken dataset must never silently become the new benchmark:
the weekly retrain workflow runs the realism suite and fails the PR on
regression (see `.github/workflows/retrain.yml`).

---

## 11. Related documents

| Document | Relationship |
| --- | --- |
| [Schema.md](technical/Schema.md) | Column reference |
| [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) | Honest scope (label noise, synthetic merchants/regions, adversarial patterns) |
| [TechSpec.md](technical/TechSpec.md) | Pipeline architecture |
| [Testing.md](technical/Testing.md) | Test strategy |
| [README.md](../README.md) | Usage + benchmark evidence |
