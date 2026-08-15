# FinSight Agent — Old Tree → New Tree

## This pass (2026-08-11)

```
Before                                After
──────                                ─────
docs/migration_summary.md      →      docs/migration/migration_summary.md
—                                     docs/module_dependency.md        (new)
—                                     docs/startup_flow.md             (new)
—                                     docs/package_overview.md         (new)
—                                     docs/migration/old_tree_to_new_tree.md (new)
—                                     docs/migration/file_move_ledger.md     (new)
```

## Prior pass (2026-08-08 v5.0 modernization)

The repository was already restructured by the v5.0 modernization pass; its
full before/after record (deletion log, move log, verification report,
DoD checklist) lives at `docs/migration/migration_summary.md` §1–§9 and
`docs/folder_structure.md` §3 (file-move log). Highlights:

- Root decluttered: `conftest.py` → `tests/conftest.py`; only canonical
  metadata + the two entry contracts (`generate_data.py`, `config.yaml`) remain
  at root.
- Domain modules consolidated under `finance_agent/`; UI under `app/`; MLOps
  under `model_bench/`; operational tooling under `scripts/`.
- Junk removed per the deletion log (with proof, per the Repository Constitution).

## No-code-move rationale (this pass)

The layout already conforms: `finance_agent/` (core), `app/` (presentation),
`model_bench/` (MLOps), `tests/` (25 files), `scripts/`, `loadtest/`,
`deploy/`, `a11y/`, `docs/`, plus canonical root metadata. This pass only
consolidates the migration record under `docs/migration/` and completes the
Phase-6 doc suite — zero code changed.
