## What & why

Weekly retrain opened by the `retrain.yml` workflow for **human review — no
auto-merge**. Refreshed the model, benchmark metadata, and model card from a
newly regenerated synthetic ledger (new seed).

## Retrain summary

- **Seed:** {{SEED}}
- **PR-AUC (holdout):** {{PR_AUC}}
- **Committed:** metadata JSON, seed counter, model bundles (+ `.sig`), results
  charts, model card
- The regenerated `data/transactions.csv` is uploaded as a workflow artifact
  (repo-hygiene G.4) — download it from this run if needed.

## Canary / shadow evaluation (Phase A.3)

The incumbent production bundle (previous commit — when one exists) and this
candidate were both scored on the **same** temporal holdout window. This table
is the per-archetype recall diff that decides whether the refresh needs human
sign-off:

{{CANARY_BODY}}

## Review checklist

- [ ] Benchmark diff in `best_model_metadata.json` reviewed (CV + holdout)
- [ ] Per-archetype recall table above reviewed — no unexpected regressions
- [ ] If the `canary-regression` label is present, the archetype regression is
      understood and explicitly accepted
