# DEAD-WEIGHT PURGE v2.0 AUDIT REPORT

**Repository**: `finsight-agent`  
**Mode**: `AUDIT_ONLY` (Zero repository modifications performed)  
**Baseline Commit SHA**: `72ed48fd84ebdfde48a7ea7ba084fbb417d0142d`  
**Audit Date**: 2026-10-11  
**Verdict**: **YELLOW** (Dead weight detected; review recommended)

---

## 1. Executive Summary

- **Tracked Files in Git**: 210
- **Untracked Local Files**: 0
- **Total LOC**: 32,486
- **Analyzed Size**: 1.54 MB
- **Total Dead-Weight Candidates Identified**: 84
  - **Tier 0 (Provable Junk/Empty)**: 61
  - **Tier 1 (High Confidence)**: 1
  - **Tier 2 (Medium / Needs Verification)**: 22
  - **Tier 3 (Protected / Hard Vetoed)**: 0

## 2. Findings by Category

| Category | Description | Count |
|---|---|---|
| **A** | Empty & Near-Empty Files | 2 |
| **C** | Junk / Caches / Build Artifacts | 59 |
| **D** | Potential Orphan Modules | 23 |
| **H** | Exact Hash Duplicates | 0 |

### Top Tier-0 Items (Provable Deletion Candidates if EXECUTE_SAFE were used):
- [`A`] `app/py.typed`: 0-byte file with no content
- [`A`] `py.typed`: 0-byte file with no content
- [`C`] `.cleanup-audit/`: Untracked build/dependency/cache artifact directory in workspace
- [`C`] `.cleanup-audit/.claude/`: Untracked build/dependency/cache artifact directory in workspace
- [`C`] `.coverage`: Untracked build/dependency/cache artifact directory in workspace
- [`C`] `.hypothesis/`: Untracked build/dependency/cache artifact directory in workspace
- [`C`] `.hypothesis/.gitignore`: Untracked build/dependency/cache artifact directory in workspace
- [`C`] `.hypothesis/constants/`: Untracked build/dependency/cache artifact directory in workspace
- [`C`] `.hypothesis/examples/`: Untracked build/dependency/cache artifact directory in workspace
- [`C`] `.hypothesis/patches/`: Untracked build/dependency/cache artifact directory in workspace
- [`C`] `.hypothesis/tmp/`: Untracked build/dependency/cache artifact directory in workspace
- [`C`] `.hypothesis/unicode_data/`: Untracked build/dependency/cache artifact directory in workspace
- [`C`] `.mypy_cache/`: Untracked build/dependency/cache artifact directory in workspace
- [`C`] `.pytest_cache/`: Untracked build/dependency/cache artifact directory in workspace
- [`C`] `.ruff_cache/`: Untracked build/dependency/cache artifact directory in workspace
_...and 46 more listed in `manifest.csv`._

## 3. Next Steps & Recommended Actions

1. Review `.purge-reports/manifest.csv` and `.purge-reports/FLAGGED.md`.
2. If safe cleanup is desired, configure `MODE: EXECUTE_SAFE` or generate an approval `PLAN.yaml`.
3. Verify that all untracked artifacts (`__pycache__`, `.venv`, `.pytest_cache`) are properly covered in `.gitignore`.
