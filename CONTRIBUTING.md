# Contributing to FinSight Agent

Thanks for wanting to help. This project is a demo/portfolio system, and it stays
credible only if code, tests, and docs stay in sync. Please read
[`docs/project/Rules.md`](docs/project/Rules.md) first — the rules there are
enforced, not aspirational.

## Ground rules

1. **Facts never reason; reasoning never computes; UI never touches data.**
   Keep the three layers separate (see `docs/technical/TechSpec.md`).
2. **Every number in an answer traces to a tool output.** Never have the LLM
   layer compute financial figures.
3. **Tests for every logic change.** Any change to `rules.py`, `features.py`,
   `tools.py`, `agent.py`, or `generate_data.py` must ship with a new or updated
   test in `tests/`.
4. **Docs change in the same commit as code.** If you change tool output shapes,
   CLI flags, config keys, or evaluation methodology, update the relevant file
   under `docs/` in the same commit. Docs that claim tests/behaviors that don't
   exist are treated as bugs.
5. **No new dependencies without discussion.** The install is intentionally
   small (no `xgboost`, no database). If you think you need one, open an issue
   first.
6. **Be honest about limitations.** If you find a gap, add it to
   `docs/KNOWN_LIMITATIONS.md` rather than hiding it.

## Development setup

```bash
make setup          # pip install -e ".[dev]"
make data           # generate data/transactions.csv
make train          # benchmark models + write the bundle + metadata
make test           # fast suite (pytest -m "not slow")
make test-slow      # server boot + 100k-row perf
make lint && make typecheck
make docs-check     # docs-vs-code consistency (schema, tests, security claims)
make hooks          # one-time: enable git hooks (pre-push lockfile-drift check)
```

## Git hooks

Run `make hooks` once per clone to point git at `.githooks/`. The pre-push hook
then recompiles `requirements.lock` from `pyproject.toml` and fails the push if
they've drifted — same check as the CI `lockfile` job, shared via
`scripts/check_lockfile.sh`. It only runs when `pyproject.toml` or
`requirements.lock` changed in the pushed range, and it skips (with a warning)
if `uv` isn't installed; CI remains the authoritative gate.

## Before opening a PR

- `ruff check . && ruff format --check .` clean.
- `mypy finance_agent model_bench` clean.
- `pytest -m "not slow"` green; run `pytest -m slow` if you touched rules/agents.
- `make docs-check` clean if you touched any docs or public behavior.
- If you changed user-visible behavior or added a feature, add a line to
  `CHANGELOG.md` under [Unreleased] in the same commit.
- If you changed `pyproject.toml` or `requirements.lock`, regenerate the
  lockfile (`uv pip compile pyproject.toml -o requirements.lock
  --python-version 3.10`) and let the pre-push hook verify it.
- If you changed the generator, `make train` output (metadata) should be updated
  in your PR if tracked numbers changed.

## Commit & PR conventions

- Conventional Commits: `fix:`, `feat:`, `docs:`, `perf:`, `test:`, `chore:`.
- Small PRs (≤ ~400 lines); squash-merge to `main`.
- One logical change per PR; never bundle a docs fix with a silent behavior change.
- Dependabot PRs (weekly `pip` + GitHub Actions bumps): review and merge promptly;
  a `pip` bump that fails the CI `lockfile` job needs a recompile of
  `requirements.lock` (`uv pip compile pyproject.toml -o requirements.lock
  --python-version 3.10`) in the same PR.

## Where things live

| Area | Path |
| --- | --- |
| Facts layer | `finance_agent/rules.py`, `features.py`, `tools.py`, `config_schema.py` |
| Reasoning layer | `finance_agent/agent.py` |
| Model benchmark | `model_bench/` |
| Streamlit app | `app/` |
| Docs | `docs/` (schema, testing, security, known limitations) |
