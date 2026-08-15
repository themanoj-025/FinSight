# Schema — FinSight Agent: Data Model

| Field | Value |
| --- | --- |
| Version | v0.2 |
| Last Updated | 2026-08-08 |
| Owner | Data Engineer |
| Status | Approved |

---

## 1. Overview

The project stores a **flat synthetic ledger** produced by `generate_data.py`
(design in [DataGeneration.md](../DataGeneration.md)): `data/transactions.csv`
for the `tiny`/`demo` tiers, `data/transactions.parquet` for the `bench` tier.
There is no external database: the schema is PaySim-derived and extended with
the data-gen v2 columns (persona / account / region / subcategory / archetype
metadata). An **optional SQLite mirror** (`data/transactions.db`,
`finance_agent/storage.py`, enabled via `data.store_path`) copies the CSV into
a `transactions` table and materializes a `risk_scores` table — a
performance/persistence layer, not a separate source of truth; the CSV stays
canonical. The `transactions` table schema is versioned by
`PRAGMA user_version` migrations (v1 → v2 added the data-gen columns below).
Derived artifacts (`model_bench/*.joblib`, `model_bench/best_model_metadata.json`)
are versioned by git + the metadata file's `training_timestamp_utc` /
`git_commit`.

> Earlier versions of this document described a fictional relational model
> (`txn_id`, `ledger_id`, `account_id`, `counterparty`, ER diagram). That model
> **does not exist in the code** and has been removed.

## 2. Column Reference (transactions.csv / transactions.parquet)

Produced by `datagen.generate_dataset()`; written with `df.to_csv(...)` /
`df.to_parquet(...)` with `index=False`. Column order is the legacy
PaySim-style set first (existing consumers), then the additive data-gen v2
columns. **29 columns total.**

| Column | Type (CSV) | Source | Description |
| --- | --- | --- | --- |
| `step` | int | generator | Hour index from the simulation start `((day - 1) * 24 + hour)`. Primary sort/time key. |
| `type` | string | generator | Transaction type: `PAYMENT`, `TRANSFER`, `CASH_OUT`, `CASH_IN`, `DEBIT`, `SHOP`, `SUBSCRIPTION`, `SALARY`. |
| `amount` | float | generator | Transaction amount (always > 0 after the balance pass; direction is implied by `type`). |
| `nameOrig` | string | generator | Originating account id (`U_Alex` for a focal persona, `U_Alex_Sav` / `U_Alex_Cred` for linked accounts, `C_BG######` for background accounts). |
| `oldbalanceOrg` | float | generator | Originating account balance **before** the transaction (clamped ≥ 0; credit accounts hold ≤ 0 debt). |
| `newbalanceOrig` | float | generator | Originating account balance **after** the transaction. |
| `nameDest` | string | generator | Destination account id / merchant id (`C_*` accounts, `M_*` merchants). |
| `oldbalanceDest` | float | generator | Destination balance before (0.0 unless the destination is a tracked persona/background account). |
| `newbalanceDest` | float | generator | Destination balance after (0.0 unless the destination is tracked). |
| `isFraud` | int (0/1) | generator | 1 for injected fraud rows (patterns 1, 4–12). |
| `isFlaggedFraud` | int (0/1) | generator | PaySim-compatible flag: `1` when `amount > 200_000`. |
| `merchant` | string | generator | Merchant / counterparty display name (synthetic catalog). |
| `category` | string | generator | Flat spending category (see §5). |
| `datetime` | string (ISO-8601, minute precision) | generator | `start_date + step hours`, e.g. `2025-01-01T08:00`. |
| `date` | string (ISO date) | generator | `datetime.date().isoformat()`, e.g. `2025-01-01`. |
| `is_focal_user` | bool | generator | `nameOrig` belongs to a focal persona (recomputed at the end of generation). |
| `is_anomaly` | int (0/1) | generator | 1 for any injected anomaly row (labeled ground truth for evaluation). |
| `anomaly_type` | string | generator | `balance_drain`, `duplicate_charge`, `spend_spike`, or empty (legacy field; the v2 classifier is `fraud_archetype`). |
| `persona_id` | string | generator | Every account's owning persona id (focal + background). |
| `persona_archetype` | string | generator | `young_professional`, `dual_income_family`, `gig_worker`, `retiree`, `recent_graduate`, `small_business_owner`. |
| `account_type` | string | generator | `checking`, `savings`, `credit`, or `background`. |
| `merchant_region` | string | generator | Region id of the merchant (= transaction region). |
| `transaction_region` | string | generator | Region id where the transaction occurred (`R##_city`). |
| `home_region` | string | generator | The persona's home region id. |
| `category_group` | string | generator | Coarse parent of `category`: `income`, `fixed`, `discretionary`, `savings`, `transfer`. |
| `subcategory` | string | generator | Leaf of the category hierarchy (e.g. `coffee_shops`). |
| `fraud_archetype` | string | generator | Which of the 15 patterns (§5 of DataGeneration.md) generated this row; `""` for legitimate rows. |
| `label_reported_at_step` | int | generator | The step at which `isFraud` becomes knowable (≥ `step`; discovery lag on ~2% of fraud rows). |
| `simulation_year` | int (derived) | generator | `date` year — convenience column for seasonality/drift analysis. |

### dtypes when loaded

`pandas.read_csv` infers: `step`/`isFraud`/`isFlaggedFraud`/`is_anomaly` as
int64, `amount`/`oldbalance*`/`newbalance*` as float64, everything else as
object/string. `is_focal_user` is parsed as bool. Consumers that need
`datetime` should convert via `pd.to_datetime(df["datetime"])`. Reading the
`bench` tier via `pd.read_parquet` preserves these dtypes natively.

## 3. Balance Invariants

Applied by the vectorized per-account clamped cumulative sum
(`datagen._balance_one`); enforced by
`tests/test_generate_data.py::test_ledger_balance_continuity` and the slow
realism suite (every focal account + account type):

- `newbalanceOrig ≈ oldbalanceOrg − amount` for debits, `≈ oldbalanceOrg + amount`
  for credits (`SALARY`/`CASH_IN`), within float tolerance; debits that would
  overdraw are **clamped** to the available balance and the row's `amount`
  reduced to match.
- Consecutive rows of the same account chain: `next.oldbalanceOrg == current.newbalanceOrig`.
- Credit accounts clamp at **0 from above** — balances are ≤ 0 (outstanding debt).
- Savings accounts only receive inflows (auto-transfers), so they grow
  monotonically (asserted in `test_data_realism.py`).

## 4. Feature Matrix (model_bench)

`finance_agent/features.py::build_features(df)` returns one row per input
transaction with **32 columns** (order stable; one-hot columns are reindexed
against the canonical type/account lists so train/test splits always align):

| Group | Features |
| --- | --- |
| Value | `amount`, `log_amount`, `amount_ratio_orig`, `amount_ratio_dest`, `balance_after_ratio`, `log_oldbalance_orig`, `log_oldbalance_dest`, `is_focal` |
| Velocity (per account) | `count_prior_orig`, `sum_amount_prev7_orig`, `steps_since_prev_orig`, `count_prior_dest`, `sum_amount_prev7_dest` |
| Context | `hour_of_day`, `amount_vs_category_mean` (trailing mean — no future leakage) |
| Causal v2 (region) | `region_distance_miles`, `is_out_of_home_region` |
| Causal v2 (first-seen) | `is_new_merchant`, `is_new_payee` |
| Causal v2 (account channel) | `account_checking`, `account_savings`, `account_credit`, `account_background` |
| Causal v2 (calendar) | `is_weekend` |
| Type one-hot | `type_CASH_IN`, `type_CASH_OUT`, `type_DEBIT`, `type_PAYMENT`, `type_SALARY`, `type_SHOP`, `type_SUBSCRIPTION`, `type_TRANSFER` |

All features are strictly backward-looking (`test_no_temporal_leakage`), never
NaN (`test_no_nans_in_numeric_features`, incl. the negative credit-balance
regression), and the causal v2 features degrade to neutral 0.0 on a legacy
frame missing the v2 columns.

## 5. Enums / Constants

| Enum | Allowed values | Where |
| --- | --- | --- |
| category | `groceries dining transport utilities entertainment shopping health subscriptions housing savings income transfer refund credit` | `finance_agent/merchants.py` (`SUBCATEGORIES` keys) |
| subcategory | per category (e.g. `dining > coffee_shops restaurants fast_food`) | `finance_agent/merchants.py` |
| category_group | `income fixed discretionary savings transfer` | `finance_agent/merchants.py::CATEGORY_GROUP` |
| type | `PAYMENT TRANSFER CASH_OUT CASH_IN DEBIT SHOP SUBSCRIPTION SALARY` | `finance_agent/constants.py` |
| account_type | `checking savings credit background` | `finance_agent/constants.py::ACCOUNT_TYPES` |
| persona_archetype | `young_professional dual_income_family gig_worker retiree recent_graduate small_business_owner` | `finance_agent/personas.py` |
| fraud_archetype | 12 fraud slugs + 3 hard-negative slugs (see DataGeneration.md §5) | `finance_agent/constants.py::FRAUD_ARCHETYPES` / `HARD_NEGATIVE_ARCHETYPES` |
| blend weights | rules/supervised/isolation_forest, sum to 1.0 | `config.yaml risk.blend` (validated by `config_schema.py`) |

## 6. Data Lifecycle

- Data regenerated on demand (`make data` / Settings page) with a fixed seed
  for reproducibility; `--seed 0` is respected. Tiers: `make data-tiny`,
  `make data-demo`, `make data-bench`.
- `bench` tier writes Parquet (`data/transactions.parquet`, gitignored) and is
  consumed only by `model_bench/train_and_compare.py` — never by `app/`.
- Artifacts: `best_model.joblib`, `risk_model_bundle.joblib`,
  `best_model_metadata.json` (CV mean ± std, per-archetype recall, cohort
  fairness, temporal stability, calibration), charts in `model_bench/results/`.

## 7. Migrations

The optional SQLite store (`finance_agent/storage.py`) uses `PRAGMA
user_version` plus an ordered in-code migration list: every schema change adds
a numbered migration instead of editing DDL in place.

| Version | Description |
| --- | --- |
| 1 | Base schema + `risk_scores` materialization |
| 2 | data-gen v2 columns: `persona_id`, `persona_archetype`, `account_type`, `merchant_region`, `transaction_region`, `home_region`, `category_group`, `subcategory`, `fraud_archetype`, `label_reported_at_step`, `simulation_year` + indexes on `account_type` / `transaction_region` / `category_group` |

Legacy CSVs missing the v2 columns are tolerated: defaults are filled at sync
time so the `NOT NULL` constraint is satisfied. The CSV itself has no schema
migrations — column changes bump the generator and are caught by the docs-code
consistency gate (`make docs-check`).

## 8. Sample Record

```json
{
  "step": 8,
  "type": "SALARY",
  "amount": 5400.0,
  "nameOrig": "U_Alex",
  "oldbalanceOrg": 0.0,
  "newbalanceOrig": 5400.0,
  "nameDest": "C_AcmeCorp Payroll",
  "merchant": "Acme Corp Payroll",
  "category": "income",
  "datetime": "2025-01-01T08:00",
  "date": "2025-01-01",
  "is_focal_user": true,
  "isFraud": 0,
  "is_anomaly": 0,
  "anomaly_type": "",
  "persona_id": "U_Alex",
  "persona_archetype": "young_professional",
  "account_type": "checking",
  "merchant_region": "R00_portland",
  "transaction_region": "R00_portland",
  "home_region": "R00_portland",
  "category_group": "income",
  "subcategory": "payroll",
  "fraud_archetype": "",
  "label_reported_at_step": 8,
  "simulation_year": 2025
}
```

## 9. Data Validation Rules

| Rule | Enforced where |
| --- | --- |
| `amount > 0` | generator (balance pass) + tests |
| balance continuity (per account, all channels) | generator + `test_ledger_balance_continuity` + realism suite |
| feature columns stable across splits | `build_features` reindex + tests |
| no NaN features (incl. negative credit balances) | `test_no_nans_*` |
| fraud rate in defensible band; per-archetype counts | realism suite + `datagen.tier_stats` |
| config keys/thresholds | `finance_agent/config_schema.py` at load time |

## 10. Sensitive Data Map

| Field | Sensitivity | Encrypted at rest? | Masked in logs? |
| --- | --- | --- | --- |
| transaction rows | synthetic | n/a | n/a |
| API key (session) | credential | never persisted | never logged |

## 11. Related Documents

| Document | Relationship |
| --- | --- |
| [DataGeneration.md](../DataGeneration.md) | Generator design (tiers, personas, fraud library) |
| [API.md](API.md) | Tool outputs derived from these columns |
| [TechSpec.md](TechSpec.md) | Pipeline |
| [PRD.md](../product/PRD.md) | Requirements |
| [AppFlow.md](../design/AppFlow.md) | Flows |
| [Design.md](../design/Design.md) | Display data |
| [ImplementationPlan.md](../project/ImplementationPlan.md) | Tasks |
| [Tracker.md](../project/Tracker.md) | Status |
| [Rules.md](../project/Rules.md) | Standards |
| [SecurityAndCompliance.md](SecurityAndCompliance.md) | Sensitive map |
| [Testing.md](Testing.md) | Data tests |
| [Deployment.md](Deployment.md) | Artifacts |
| [Glossary.md](../reference/Glossary.md) | Vocabulary |
| [RiskRegister.md](../project/RiskRegister.md) | Risks |
| [KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md) | Honest scope |
