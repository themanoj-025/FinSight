# Model Card — FinSight Agent risk-scoring model

> **Auto-generated** from `model_bench/best_model_metadata.json` by `model_bench/train_and_compare.py`. Do not hand-edit — every figure below is read from the benchmark's own metadata.

## Model identity
- **Algorithm:** Gradient Boosting (LightGBM)
- **Task:** per-transaction fraud-risk scoring (rare positive class, binary)
- **Selection:** pr_auc (mean over TimeSeriesSplit CV) — TimeSeriesSplit(k=5)
- **Selection policy:** best mean CV PR-AUC, preferring the SHAP-capable model (Gradient Boosting (LightGBM)) when within 1 CV std of the leader (explainability tie-break, see KNOWN_LIMITATIONS)
- **Training timestamp (UTC):** 2026-08-08T11:51:50.779341+00:00
- **Git commit:** 75af5adce963176cca405dafd200aa7ee35f1b4a
- **Bundle signature:** none (key origin: n/a)

## Intended use
- **Primary:** explainable per-transaction fraud-risk scoring on the synthetic
  FinSight ledger, blended with audit rules + an isolation-forest anomaly score
  into one risk score. Not for use on real financial data.
- **Out of scope:** autonomous blocking/declining decisions; any deployment on
  real accounts; regulatory decisioning. The model is a demo-grade, synthetic-only
  artifact (see docs/KNOWN_LIMITATIONS.md).

## Training data
- **Provenance:** synthetic (deterministic generator, seed 42),
  no real PII.
- **Size:** 10,700,142 rows (600,000 train /
2,675,036 temporal holdout), fraud rate 0.1%.
- **Personas:** 20200 total (200 focal)
from 6 archetypes.
- **Features:** 32 strictly backward-looking
features (no temporal leakage).

## Performance
- **CV (mean ± std):** PR-AUC 0.8284 ± 0.0554 · ROC-AUC 0.9975 ±
0.0007 · F1 0.7328 ±
0.0660
- **Holdout:** precision 0.1024; recall 0.8615; f1 0.1830; roc auc 0.9980; pr auc 0.7282.

### Per-archetype recall (holdout) — the interview-relevant view

| Archetype | Recall @ 0.5 | Support |
| --- | ---: | ---: |
| account_takeover | 1.0000 | 48 |
| balance_drain | 1.0000 | 153 |
| card_testing | 0.9951 | 205 |
| mimicry | 0.2121 | 99 |
| new_payee_transfer | 0.8649 | 74 |
| refund_abuse | 0.7376 | 343 |
| seasonal_mimicry | 0.4750 | 80 |
| slow_balance_drain | 1.0000 | 402 |
| subscription_creep | 1.0000 | 192 |

**Known failure modes:** the adversarial tier (mimicry, account takeover,
seasonal mimicry) has materially lower recall than the easy/medium tiers —
that is by design (difficulty-graded fraud library) and is stated honestly here.
A deployment would pair this model with the rule detectors, which are what catch
most structural fraud.

### Cohort fairness (persona archetypes)

| Cohort | Recall | Support |
| --- | ---: | ---: |
| dual_income_family | 0.8349 | 218 |
| gig_worker | 0.8150 | 200 |
| recent_graduate | 0.9282 | 209 |
| retiree | 0.8408 | 402 |
| small_business_owner | 0.8717 | 265 |
| young_professional | 0.8841 | 302 |

## Calibration
- **Brier score:** 0.0032
- **Expected calibration error (ECE):** 0.0056

## Ethical considerations
- The dataset is **fully synthetic** — no real PII, no real accounts, no real
  transactions. Nothing learned here transfers to real-world data without
  re-validation.
- Per-cohort recall is published above so model disparity is visible, not hidden.
- The risk score is **not** a financial decision. It exists to demonstrate
  explainable fraud detection on synthetic data.
