# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| v0.1.x | ✅ (until v0.2.0) |
| < 0.1 | ❌ |

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Email the maintainer
(private) or open a [private vulnerability report](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability).

You should receive a response within 5 business days.

## Scope — please read this first

This project is a **demo/portfolio system running on synthetic data**. Its
threat model is correspondingly small, but it has real, documented gaps that you
should know about before deploying it anywhere:

- **Auth is demo-grade.** `APP_PASSWORD` provides a shared-password gate only —
  no accounts, OAuth, or RBAC. See `app/common.py::require_auth` and
  [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).
- **No database.** The ledger is a CSV; model bundles are joblib pickles loaded
  via `joblib.load`. Only load bundles produced by this project's own training
  pipeline — loading a pickle executes arbitrary code.
- **Synthetic data only.** No real PII is stored, and the API key is held only in
  browser session state and never persisted.

The "no real PII" property is by design, not an accident: this project does not
need to process real financial data, and it never will without a full
security re-design.

## What we fix

We treat these as in-scope:

- Prompt-injection / LLM abuse that can extract non-public data or cause cost abuse
  (session budgets exist; see `agent.max_session_turns`/`max_session_tokens`)
- Config validation gaps (`finance_agent/config_schema.py`)
- Any accidental persistence of the Anthropic API key
- Docs that overstate the security posture
- Known-vulnerable dependencies (caught by `pip-audit -r requirements.lock` in
  CI and by Dependabot's weekly `pip` + GitHub Actions update PRs)

We will **not** fix, without a much larger design discussion: missing
multi-user auth, TLS termination (expected at your reverse proxy), or supply-chain
signing of model bundles (documented as a limitation, not silently shipped).

## Dependency hygiene

- `pip-audit -r requirements.lock` runs in CI on every push and fails the build
  on a known-vulnerable dependency (base dependencies only).
- Dependabot opens weekly update PRs for the `pip` ecosystem (via
  `pyproject.toml`) and for GitHub Actions. `pip` bumps that shift the resolved
  set require recompiling `requirements.lock` — the CI `lockfile` job and the
  pre-push hook (`make hooks`) enforce this.

## Known limitations

Everything that is simplified or not production-grade is listed honestly in
[docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). If you find a gap that
is not listed there, that is itself a documentation bug — please report it.
