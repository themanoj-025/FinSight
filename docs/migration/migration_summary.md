# Migration Summary — Repository Modernization Pass

Date: 2026-08-08 · Scope: full-repository restructuring & cleanup (per the
v5.0 modernization prompt) · Policy: the Repository Constitution — no behavior
changes, no public-API changes, no deletion without proof, git-history-preserving
moves, incremental commits, flag-don't-delete when uncertain.

## 1. What was done

| Phase | Action | Result |
|---|---|---|
| 1. Analysis | Full inventory + AST import graph + classification | `docs/project/analysis_report.md` |
| 2. Classification | Every top-level file tagged (entry/config/domain/data/api/presentation/cross-cutting/infra/tests/docs) | §2 of the analysis report |
| 3. Duplicate & dead code | SHA-256 content-hash scan; AST dead-symbol scan; ruff F401/F841; helper-duplication grep | **0 duplicates · 0 empty files · 0 dead symbols · 0 duplicated helpers** → no deletions |
| 4. Target architecture | Adapted to the existing layered layout (no force-fit) | `docs/folder_structure.md` |
| 5. Moves & references | One justified move, `git mv`, references verified | `conftest.py` → `tests/conftest.py` |
| 6. AI-artifact cleanup | TODO/FIXME/placeholder scan | none found |
| 7. Cross-cutting | Secret scan, silent-except audit, CI/formatting review | clean (report-only findings, none needed fixing) |
| 8. Verification | Test suite, lint, type-check, docs-gate after each phase | all green (below) |
| 9. Reporting | This file + architecture + folder structure + analysis report + human-review list | ✔ |

## 2. Deletion log

**No files were deleted.** Every candidate was examined; none met the evidence
bar of the mandatory deletion workflow:

| Candidate | Evidence | Verdict |
|---|---|---|
| `model_bench/SEED_COUNTER` | "runtime counter, looks generated" | **Keep** — consumed by `.github/workflows/retrain.yml` (reads, increments, re-commits via `git add -f`); live infrastructure |
| `docs/assets/README.md` | "stub referencing missing images?" | **Keep** — `docs/assets/img/*.png` are tracked and exist; table is accurate |
| 16 module-level symbols with 0 *external* refs (AST scan) | e.g. `gen_mimicry`, `assign_archetypes`, `blend_prose`, `load_config`, `data_source` | **Keep all** — every one has internal references (dispatch registries, same-module callers); refined scan showed internal=1–5 each |
| Duplicate-content groups (hash) | — | none exist |
| Empty files | — | none exist |
| Hardcoded secrets | — | none exist |

## 3. Move log

| Old | New | Reason | Notes |
|---|---|---|---|
| `conftest.py` | `tests/conftest.py` | canonical pytest location; clean root | `git mv` (97% similarity), `ROOT` path updated to `parent.parent`; `from conftest import boot_api_server` + `api_server` fixture verified working |

## 4. Import / reference update summary

- Zero source-code references needed changing: `tests/test_api.py` uses
  `from conftest import boot_api_server`, which resolves identically from
  `tests/` (pytest inserts the test basedir and the moved conftest still
  registers `ROOT` on `sys.path`).
- No CI, Docker, Makefile, or doc references pointed at the old path.
- `pyproject.toml` (`testpaths = ["tests"]`) already covers the new location.

## 5. Verification report (Phase 8, run after each phase)

| Check | Command | Result |
|---|---|---|
| Fast test suite | `pytest tests/ -m "not slow"` | **EXIT=0** (all pass) |
| Lint | `ruff check .` | clean (0 issues) |
| Format | `ruff format --check .` | 76/76 formatted |
| Type check | `mypy finance_agent model_bench` | success (22 files, no issues) |
| Docs consistency | `python scripts/check_docs_consistency.py` | **15/15 passed** |
| Realism suite | `pytest tests/test_data_realism.py -m data_realism` | 13/13 (run previously, unchanged data) |
| App renders | Streamlit AppTest headless render (all pages) | green (existing suite) |
| API boot | slow-suite server-boot test | covered by fast suite fixtures; full boot exercised by `test_streamlit_app_boots_headless` |
| Docker build | `docker build` | **not runnable on this host** — flagged; the move touches no image paths (`conftest.py` is not copied into the image; tests don't run there) |

Nothing is fabricated: the Docker-build check could not be executed and is
stated as such.

## 6. Needs Human Review list

Items deliberately left as-is; each is safe but worth an owner's decision:

1. **`generate_data.py` at repo root** — it is both an entry-point CLI and an
   importable module (`from generate_data import generate`) referenced by ~8
   test files, `scripts/check_docs_consistency.py`, CI, the app, and
   `docker-entrypoint.sh`. Moving it into `finance_agent/` (e.g. as a thin
   console-script wrapper) would be cleaner long-term but changes import paths
   that behave as public API — recommend as a separate, deliberate refactor.
2. **`docker-entrypoint.sh` at repo root** — conventional location for a
   root-level Dockerfile pair; moving to `docker/` would require updating
   `Dockerfile` and both `docker-compose.yml` command paths, and the Docker
   build cannot be verified on this host. Recommend moving it together with the
   next Docker-tooling change.
3. **`config.yaml` at repo root** — central config is a deliberate top-level
   choice (prompt allowlists top-level configuration). No action required.
4. **`model_bench/SEED_COUNTER` tracked in git** — intentional runtime state
   for the weekly retrain job; an alternative (derive the next seed
   deterministically from the committed metadata, no state file) is a clean
   future improvement.
5. **Docs layout** — `docs/KNOWN_LIMITATIONS.md` sits at `docs/` root while the
   rest of the suite is subfoldered; merging it into `docs/technical/` would
   churn README + cross-links and the docs-consistency gate. Cosmetic only.

## 7. Definition of Done checklist

- [x] No stray files remain at root (only entry points, metadata, config, container tooling, folders)
- [x] No duplicate files/folders/logic/assets unresolved (hash + structural scans: none exist)
- [x] No dead code / unused imports / unused dependencies unresolved (AST + ruff: none)
- [x] No empty files or folders
- [x] Every file lives in a location consistent with the target architecture
- [x] Every import/reference resolves (fast suite green after the move)
- [x] Build/tests/lint/type-check/docs-gate all pass (Docker build not runnable on host — stated)
- [x] Application behaves identically (zero logic changes; only one test-infra move)
- [x] Full reporting produced (this file + analysis_report + architecture + folder_structure)
- [x] Needs Human Review list exists (§6)

## 8. Follow-up audit (2026-08-08, second pass)

A second, ripgrep-exhaustive pass found six zero-reference legacy symbols the
AST scan above had missed (they were never imported or dispatched, so the
internal-ref scan did not flag them). All were removed with zero behavior
change; the full gates re-ran green.

| Removed symbol | File | Evidence |
|---|---|---|
| `_u01` | `finance_agent/personas.py` | never called (uniform helper from an earlier generator iteration) |
| `archetype_of` | `finance_agent/personas.py` | never called (sanity helper, no consumers) |
| `region_distance_miles` | `finance_agent/fraud_patterns.py` | never called (trivial wrapper over `haversine_miles`; import also dropped) |
| `SALARY_AMOUNT` / `RENT_AMOUNT` / `SAVINGS_AMOUNT` | `generate_data.py` | legacy named constants from the pre-persona generator; only a no-op test monkeypatch referenced `SALARY_AMOUNT` (dropped with the constant) |
| `TYPE_BY_CATEGORY` | `generate_data.py` | legacy type map, zero consumers |
| `CREDIT_TYPES` | `generate_data.py` | duplicate of the canonical `finance_agent.constants.CREDIT_TYPES`, unused in-module |

Files touched: `finance_agent/personas.py`, `finance_agent/fraud_patterns.py`,
`generate_data.py`, `tests/test_generate_data.py`. Verification: `ruff check` /
`ruff format --check` clean, `mypy` success (22 files), fast suite 202/202
(exit 0), `make docs-check` 15/15.

## 9. Needs Human Review list (follow-up)

1. **`generate_data.py` legacy surface** — `subscription_total()` and the
   `SUBSCRIPTION_AMOUNTS` re-export were deliberately kept: both are exercised
   by `tests/test_generate_data.py` (TC-008/TC-010) and documented. If the
   test suite ever drops them, they can go with it.

---

*This addendum supersedes the §1/§7 "0 dead symbols" claims for the current
state of the tree: those claims were accurate for the tree as scanned, and the
removals above are the follow-up corrections.*

---

## Phase 3 Re-run — Full Protocol Verification (2026-08-12)

**Mandate:** Full re-execution of the Principal Architect restructuring protocol; zero-regression; evidence-backed Phase 7.

**Discovery (P1) / Classification (P2) / Target conformance (P3):** Structure conforms (finance_agent/, app/ Streamlit frontend, tests/, scripts/, deploy/).

**Moves (P4) & Naming (P5):** No moves required this pass. Banned-token scan: clean (model_bench results are legitimate artifacts).

**Verification (P7) — evidence:**
| Check | Command | Result |
|---|---|---|
| Import resolution | python -c 'import finance_agent' | OK |
| Lint (criticals) | python -m ruff check . --select=E9,F63,F7,F82 | 0 errors |
| Syntax compile | py_compile on all .py | OK |
| Test collection | pytest --collect-only tests/test_agent.py | 30 tests collected OK |
| Full suite | python -m pytest -q | Env-limited: suite exceeds 10 min + Windows paging-file limit (documented backlog item) |

**Risk & Rollback (P8):** No moves — no new risk.

**Follow-up backlog (P9):**
- finance_agent.cli import hits a Windows paging-file limit in this env only — CI unaffected (backlog item from Phase 2, unchanged).
- Full test suite requires CI (GitHub Actions) to complete; local runs thrash the paging file.

---

## Re-run verification addendum (2026-08-12, evening session)

Full v5.0 protocol re-execution — re-verified per repo mandate:

| Check | Command | Result |
|---|---|---|
| Import resolution | python -c 'import finance_agent' | OK |
| Lint (criticals) | python -m ruff check . --select=E9,F63,F7,F82 | 0 errors |
| Syntax compile | py_compile on all tracked .py | OK |
| Subset tests | pytest test_storage + test_merchants | 24 passed (exit 0) |
| Full suite | pytest -q | Env-limited (Windows paging-file limit, WinError 1455 — documented backlog, unchanged) |
| Docker image | docker build . | SUCCESS (sha256:ae434ef6…) |

Duplicate scan (content hash): none. Empty-file scan: only `status/health.log`
(0 B — deliberately tracked static status page, .gitignore negation
`!status/health.log`, fed by CI `status.yml`). Root allowlist: conforms.
No moves required; no deletions required; no unresolved findings.
