# finsight-agent — Documentation Folder Cleanup & De-LLM-ification Audit (2026-08-15)

## 1. Executive Summary

Scope: full `docs/` tree — root docs (DATASHEET, DEMO_SCRIPT,
KNOWN_LIMITATIONS, DataGeneration), `design/`, `product/`, `project/`,
`reference/`, `technical/` (incl. OpenAPI spec, MutationTesting, SLOs),
`assets/` (images + README), `migration/`, `audit/`. Docs are specific to the
actual agent (real endpoints, SLOs, mutation-testing results, datasheet).
Reads as human-curated. No Tier 0/1 actions required.

## 2. Urgent: Leaked Secrets/Credentials Found

None.

## 3. LLM/AI Fingerprints Removed

None. "Acme Corp Payroll" in `technical/Schema.md` is example payload data in
a synthetic-dataset sample, not an unreplaced placeholder.

## 4. Structural Changes

None. `assets/img/*.png` are real screenshots, indexed by `assets/README.md`;
`technical/openapi.v1.json` is toolchain-generated reference (preserved).

## 5. Duplicate Content Consolidated

None. No identical files, no same-basename collisions.

## 6. Contradictions Found (manual review, not auto-resolved)

None found.

## 7. Boilerplate/Template Cruft Removed

None.

## 8. Dead Links Fixed/Removed

None. Link scanner clean.

## 9. README / CONTRIBUTING / CONSTITUTION Review

No `docs/README.md` index; the repo-root README links 8+ docs files directly
— effective entry-point coverage.

## 10. Security/Privacy Findings

None.

## 11. Consistency Fixes Applied

None required.

## 12. Files Modified

- `docs/audit/cleanup-audit-2026-08-15.md` — added (this report)

## 13. Files/Folders Deleted

None.

## 14. Remaining Manual Review Items

1. **Pending demo artifact (Tier 2, owner action)** — `DEMO_SCRIPT.md`
   instructs rendering `docs/assets/finsight_demo.mp4/.gif` and embedding them
   in the README, but those files are not yet committed (only the three PNG
   screenshots exist). This is a deliberate "render and commit" step in the
   script, not a broken link — owner must produce the demo video.
2. **No docs index (Tier 2 recommendation)** — optional `docs/README.md`;
   repo-root README currently covers entry points.

## 15. "Does This Still Look AI-Scaffolded?" Score

**99 / 100** — no empty folders, no contradictions, SLOs and datasheet carry
real specifics. −1 for the optional index recommendation.
