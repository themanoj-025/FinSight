# SecurityAndCompliance — FinSight Agent: Security

| Field | Value |
| --- | --- |
| Version | v0.4 |
| Last Updated | 2026-08-10 |
| Owner | Security Engineer |
| Status | Approved |

> This document describes what is **actually implemented**. Anything not
> implemented is listed under "Planned / Not Implemented" — the project
> deliberately avoids claiming controls that don't exist.
> See [KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md) for the plain-language scope.

---

## 1. Threat Model (STRIDE)

| Threat | Surface | Impact | Mitigation (implemented) |
| --- | --- | --- | --- |
| Spoofing | Agent prompt injection | Misleading answers | System prompt guardrails (`agent.py` SYSTEM_PROMPT): tool-output-only answers, merchant/transaction content treated as untrusted data, out-of-scope refusals |
| Tampering | Config | Wrong weights | `finance_agent/config_schema.py` validates config at load time (`ConfigError` names the bad key) |
| Info disclosure | API key | Cost/abuse | Session-only (never persisted); invalid keys are detected before "connected" is shown (`validate_api_key`) |
| DoS | LLM loop | Cost | 5-turn tool bound + per-session turn/token budget (`agent.max_session_turns` / `max_session_tokens`) with offline fallback |
| Supply chain | `joblib.load` of model bundles | Arbitrary code execution if a bundle is tampered with | **Mitigated (C.2.4):** every bundle is HMAC-SHA256 signed at train time (`finance_agent/bundle_security.py`); the signature is verified with `hmac.compare_digest` before `joblib.load`, and a mismatch is refused loudly with a rule-only fallback |
| Elevation | — | — | N/A (single-user demo app) |

### 1.1 Trust boundaries — per-boundary STRIDE walkthrough (C.2.7)

The system has exactly three trust boundaries. Each row names the threats that
apply *at that boundary* and what is mitigated vs. explicitly **accepted for
demo scope** — the value is that the analysis is done and visible, not that
every risk is closed.

| Boundary | Spoofing | Tampering | Repudiation | Info disclosure | DoS | Elevation |
| --- | --- | --- | --- | --- | --- | --- |
| 1. Browser ↔ Streamlit app | Optional `APP_PASSWORD` gate, compared with `hmac.compare_digest`; visible "DEMO MODE — NOT SECURED" banner when unset | UI is read-mostly; all downstream inputs re-validated (config schema, tool input checks) | — | No real PII by design (synthetic ledger); chat history session-scoped | Reload resets session state by design (accepted) | N/A — single-user demo app (accepted) |
| 2. Streamlit/agent ↔ Anthropic API | API key validated before "connected" is shown; session-only, never persisted | System-prompt guardrails: tool-output-only answers, merchant/transaction content treated as untrusted data, out-of-scope refusals | Anthropic-side (out of scope) | Payload minimized (tool outputs only; history capped) | 5-turn tool bound + per-session turn/token budget (`agent.max_session_turns` / `max_session_tokens`), persisted in SQLite — survives reload | N/A — model outputs are data, not authority |
| 3. App ↔ facts API / SQLite | Optional `X-API-Key` shared-secret gate, compared with `hmac.compare_digest`; auth failures logged | Config validated at load (`config_schema.py` names the bad key); bundles HMAC-SHA256 signed before `joblib.load`; all queries parameterized | — | Synthetic data only; API responses paginated + `risk-scored` `limit` bounded; optional per-IP rate limiting (`FINSIGHT_RATE_LIMIT_PER_MIN`, off by default); security headers (`nosniff`/`X-Frame-Options`/`Referrer-Policy`) on every response; CORS locked via `FINSIGHT_CORS_ORIGINS` (permissive `*` only as the local-dev default, `allow_credentials=False`) | N/A — no privileged operations |

## 2. Auth / Authorization

- **Implemented:** optional demo-grade password gate — if `APP_PASSWORD` is set,
  every page requires it (`app/common.py::require_auth`); if unset, the app shows
  a visible "DEMO MODE — NOT SECURED" banner instead of pretending to be safe.
- **Implemented:** optional shared-secret gate for the facts API — if
  `FINSIGHT_API_KEY` is set, every `/api/*` request must carry an `X-API-Key`
  header (`finance_agent/api.py`); the app's `ApiClient` sends it automatically.
  Demo-grade only, same caveats as `APP_PASSWORD`. Failed auth attempts are
  logged with request context (IP, path, correlation id) — never the secret.
- **Implemented (audit §5):** per-session brute-force cooldown on the app
  password gate (5 failures / 15 min → 5-min lockout, `app/common.py`).
  IP-level throttling remains a reverse-proxy concern.
- **Implemented (audit §5/§6):** optional per-IP rate limiting on `/api/*`
  (`FINSIGHT_RATE_LIMIT_PER_MIN`, in-process sliding window, 429 +
  `Retry-After`; off by default), configurable CORS allow-list
  (`FINSIGHT_CORS_ORIGINS`), and baseline security headers
  (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`) on every API
  response. CSP is intentionally deferred to the reverse proxy (a strict
  policy would break the Swagger UI's inline scripts) — ready-made templates
  are provided at `deploy/` (`Caddyfile.example` / `nginx.conf.example`, see
  DEPLOY.md §9), adopt or adapt.
- **Deliberate decision (audit §5, permanently out of scope):** real
  authentication (accounts, OAuth, RBAC) is **not built and will not be** for
  this project. The shared-secret gates (`APP_PASSWORD` for the UI,
  `FINSIGHT_API_KEY` for the API) are a *permanent, intentional* design
  choice, not a known gap awaiting work: the product is a synthetic-data
  portfolio demo with a single/small persona population and no real user data
  or multi-tenant need — per-user accounts would add real security surface
  (password hashing, reset flows, session invalidation, a user table) with no
  product benefit, against the project's own stated engineering values. If
  this codebase is ever repurposed into a real multi-user product, real auth
  gets its own dedicated spec (recommendation: FastAPI Users + JWT, scoped
  to email+password signup/login/logout and a single owner role) and the
  audit checklist's auth row would be re-run against that new code.
- Anthropic API key entered in Settings (session-only), validated before use.

## 3. Data Classification

| Data | Class | Handling |
| --- | --- | --- |
| Transaction ledger | synthetic | reproducible, no real PII |
| API key | credential | session-only, never written to disk or logs |
| Chat history | internal | session-scoped (`st.session_state`), capped before sending to the LLM |

## 4. Encryption

- In transit: TLS to Anthropic.
- At rest: nothing sensitive persisted (synthetic data only; key is never persisted).

## 5. Compliance Checklist (implemented)

- [x] No real PII by design (synthetic)
- [x] API key never persisted
- [x] LLM inputs minimized (tool outputs only; history capped)
- [x] Config validation at load time (typo'd keys raise a named `ConfigError`)
- [x] Session LLM cost caps with visible fallback
- [x] API key validated before claiming "connected"
- [x] Prompt-injection guardrails in the system prompt
- [x] Optional `X-API-Key` gate on the facts API (`FINSIGHT_API_KEY`)
- [x] Dependency scans (`pip-audit -r requirements.lock` in CI — fails on a known-vulnerable dependency)
- [x] Signature/checksum verification of model bundles — HMAC-SHA256 signed at
      train time and verified with `hmac.compare_digest` before `joblib.load`
      (`finance_agent/bundle_security.py`, C.2.4); a tampered bundle is refused
      loudly with a rule-only fallback. **The well-known demo default key
      (`finsight-demo-bundle-key-2026-change-me`) is for LOCAL DEVELOPMENT
      ONLY — it is public (it ships in this repo), so it only stops accidental
      corruption or casual tampering, not an adversary with repo access. Any
      real deployment MUST set `FINSIGHT_BUNDLE_KEY` to a unique generated
      value (see `scripts/generate_secrets.sh` / DEPLOY.md) and re-sign the
      bundle on the target. When a real key is set, a bundle whose signature
      doesn't verify against it aborts the API at startup with a
      `BundleSignatureError` naming the fix (audit §2) — it is never silently
      accepted or silently downgraded to rule-only.**
- [x] Optional per-IP rate limiting on `/api/*` (`FINSIGHT_RATE_LIMIT_PER_MIN`,
      429 + `Retry-After`; off by default — see KNOWN_LIMITATIONS.md §21)
- [x] Security headers on every API response + CORS allow-list
      (`FINSIGHT_CORS_ORIGINS`; `*` only as the local-dev default with
      `allow_credentials=False`)
- [x] HTML escaping of data-driven strings rendered via `unsafe_allow_html`
      (`app/common.py::esc`) — defense-in-depth on the synthetic-ledger UI
- [x] Auth failures logged with request context; internal exception text never
      returned to the LLM/client (generic messages only)

## 6. Incident Response Plan (outline)

1. Detect: cost spike / CI failure.
2. Triage: key leak vs bug.
3. Contain: revoke key / rotate `APP_PASSWORD`.
4. Remediate + tests.
5. Recover.
6. Postmortem.

## 7. Related Documents

| Document | Relationship |
| --- | --- |
| [Rules.md](../project/Rules.md) | Security rules |
| [API.md](API.md) | Tool contracts |
| [Schema.md](Schema.md) | Sensitive map |
| [TechSpec.md](TechSpec.md) | NFRs |
| [PRD.md](../product/PRD.md) | Goals |
| [AppFlow.md](../design/AppFlow.md) | Flows |
| [Design.md](../design/Design.md) | Design |
| [ImplementationPlan.md](../project/ImplementationPlan.md) | Tasks |
| [Tracker.md](../project/Tracker.md) | Status |
| [Testing.md](Testing.md) | Security tests |
| [Deployment.md](Deployment.md) | Secrets |
| [Glossary.md](../reference/Glossary.md) | Vocabulary |
| [RiskRegister.md](../project/RiskRegister.md) | Risks |
| [KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md) | Honest scope |
