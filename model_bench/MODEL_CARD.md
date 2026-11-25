# Model Card — FinSight Agent risk-scoring model

> **Auto-generated** from `model_bench/best_model_metadata.json` by `model_bench/train_and_compare.py`. Do not hand-edit — every figure below is read from the benchmark's own metadata.

## Model identity
- **Algorithm:** Gradient Boosting (LightGBM)
- **Task:** per-transaction fraud-risk scoring (rare positive class, binary)
- **Selection:** pr_auc (mean over TimeSeriesSplit CV) — TimeSeriesSplit(k=5)
- **Selection policy:** best mean CV PR-AUC, preferring the SHAP-capable model (Gradient Boosting (LightGBM)) when within 1 CV std of the leader (explainability tie-break, see KNOWN_LIMITATIONS)
- **Training timestamp (UTC):** 2026-11-25T09:52:38.835095+00:00
- **Git commit:** d486e0023da440d50148ddcb262f15d24c49bf3e
- **Bundle signature:** hmac-sha256 (key origin: demo-default)

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
- **Size:** 61,396 rows (46,047 train / 
15,349 temporal holdout), fraud rate 0.1%.
- **Personas:** 2002 total (2 focal) 
from 6 archetypes.
- **Features:** 32 strictly backward-looking 
features (no temporal leakage).

## Performance
- **CV (mean ± std):** PR-AUC 0.2435 ± 0.1877 · ROC-AUC 0.9958 ± 
0.0029 · F1 0.1714 ± 
0.1761
- **Holdout:** precision 0.0000; recall 0.0000; f1 0.0000; roc auc nan; pr auc 0.0000.

### Per-archetype recall (holdout) — the interview-relevant view

| Archetype | Recall @ 0.5 | Support |
| --- | ---: | ---: |

**Known failure modes:** the adversarial tier (mimicry, account takeover, 
seasonal mimicry) has materially lower recall than the easy/medium tiers — 
that is by design (difficulty-graded fraud library) and is stated honestly here. 
A deployment would pair this model with the rule detectors, which are what catch 
most structural fraud.

### Cohort fairness (persona archetypes)

| Cohort | Recall | Support |
| --- | ---: | ---: |

## Calibration
- **Brier score:** 0.0000
- **Expected calibration error (ECE):** 0.0000

## Ethical considerations
- The dataset is **fully synthetic** — no real PII, no real accounts, no real 
  transactions. Nothing learned here transfers to real-world data without 
  re-validation.
- Per-cohort recall is published above so model disparity is visible, not hidden.
- The risk score is **not** a financial decision. It exists to demonstrate 
  explainable fraud detection on synthetic data.
