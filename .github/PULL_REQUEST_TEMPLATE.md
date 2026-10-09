## What & why

<!-- One or two sentences: what changed and why. Reference issues with #NN. -->

## Type of change

- [ ] fix
- [ ] feat
- [ ] docs
- [ ] perf
- [ ] test
- [ ] chore

## Checklist

- [ ] `make lint` clean (`ruff check . && ruff format --check .`)
- [ ] `make typecheck` clean (`mypy finance_agent model_bench`)
- [ ] `make test` (fast suite) passes
- [ ] `pytest -m slow` passes **if** rules/agent/generator code changed
- [ ] New/updated tests accompany every logic change
- [ ] `docs/` updated in the **same commit** if public behavior changed
      (tool output shapes, CLI flags, config keys, evaluation methodology)
- [ ] No new dependencies without prior discussion (see CONTRIBUTING.md)
- [ ] If this reveals a new limitation, `docs/KNOWN_LIMITATIONS.md` is updated

## Verification notes

<!-- What did you run to verify? Paste key output (test counts, lint results). -->
