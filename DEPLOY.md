# Deploying FinSight Agent — runbook (Phase E.1)

This is the **§3a agent-prepared** side of the live-demo story. Everything
below is copy-paste; the only human-required part is provisioning the account
and running the final commands (§3b). Target: Fly.io (primary), Railway
(alternative). The deploy intentionally ships **without** an Anthropic key —
the public instance runs in BYO-key / offline-narrator mode by default and
banners that clearly on the Ask-the-Agent page (`FINSIGHT_PUBLIC=1`).

---

## 0. What is already in the repo (agent-done)

| Artifact | Purpose |
|---|---|
| `fly.toml` | Two-process app (`app` = Streamlit :8501, `api` = FastAPI :8000) with persistent volumes at `/app/data` + `/app/model_bench` |
| `Dockerfile` | Reused as-is — digest-pinned base, non-root user, entrypoint bootstrap (already deploy-grade; no second Dockerfile needed, so no drift) |
| `docker-entrypoint.sh` | Idempotent bootstrap + fingerprint guard: generates data / trains only when artifacts are missing, and retrains only when the ledger is newer than the bundle |
| `.github/workflows/status.yml` | 15-minute health ping → `status/health.log` (SLO tracking, D.2). No-op until the `STATUS_URL` variable is set (one-line activation) |
| BYO-key banner | `app/pages/4_Ask_The_Agent.py` shows a clear "bring your own key" notice when `FINSIGHT_PUBLIC=1` and no API key is present |

---

## 1. Prerequisites (human, ~10 min)

1. Create a [Fly.io](https://fly.io) account (free hobby tier is enough for the
   demo tier dataset).
2. Install `flyctl` and sign in:
   ```bash
   fly auth login
   ```

## 2. Provision the app + volumes

```bash
# From the repo root.
fly apps create finsight-agent          # or your own name — then update fly.toml `app` line
fly volumes create data_volume --region iad --size 1
fly volumes create model_volume --region iad --size 1
```

## 3. Set secrets (never bake keys into the image)

**Don't improvise these values** — generate strong, unique ones with the
secrets runbook (audit §1a):

```bash
./scripts/generate_secrets.sh   # prints APP_PASSWORD / FINSIGHT_API_KEY / FINSIGHT_BUNDLE_KEY
# Store the three printed values in your password manager NOW — the script
# does not persist them. Then:

fly secrets set APP_PASSWORD='<generated-APP_PASSWORD>'
# Demo-grade password gate for the Streamlit UI (see docs/KNOWN_LIMITATIONS.md).
fly secrets set FINSIGHT_API_KEY='<generated-FINSIGHT_API_KEY>'
# Shared secret for the API (X-API-Key header). Without it, /api/* is open to
# anyone who can reach :8000 — set it on any public deployment.
fly secrets set FINSIGHT_BUNDLE_KEY='<generated-FINSIGHT_BUNDLE_KEY>'
# Model-bundle signing key. The repo's demo default is PUBLIC (local dev
# only) — with a real key set, the API refuses to boot on a bundle signed
# with any other key (audit §2). Re-sign the bundle on the target so it
# matches: `fly ssh console -C "FINSIGHT_BUNDLE_KEY='...' make train"`.
```

Then **prove the gates are enforced** (self-checking, not "hope it worked"):

```bash
FINSIGHT_URL=https://<app>.fly.dev:8000 FINSIGHT_API_KEY='<generated-FINSIGHT_API_KEY>' make verify-secrets
# PASS: 401 without the key, 200 with it, app reachable (browser password-prompt check listed)
```

Rate limiting + CORS are regular env vars (already in fly.toml): the API
ships with `FINSIGHT_RATE_LIMIT_PER_MIN=600` and a locked-down
`FINSIGHT_CORS_ORIGINS` — adjust them in fly.toml if your app name differs.
Deliberately NOT set: `ANTHROPIC_API_KEY`. The public instance must run in
offline-narrator / BYO-key mode. Users paste their own key in the app's
Settings page if they want the LLM agent.

## 4. Deploy

```bash
fly deploy
```

First boot generates the demo ledger and trains/benchmarks the models onto the
volumes (~2–4 min; the health checks have generous grace periods for this).
Subsequent deploys reuse the artifacts — the entrypoint sees them and starts
instantly.

Verify:

```bash
fly open                          # Streamlit UI (https://<app>.fly.dev)
curl -s https://<app>.fly.dev:8000/api/v1/health
```

## 5. Activate SLO monitoring (one variable flip)

1. GitHub repo → Settings → Secrets and variables → Actions → **Variables**.
2. Add `STATUS_URL = https://<app>.fly.dev:8000/api/v1/health`.
3. The scheduled `status-page` workflow starts appending to `status/health.log`
   within 15 minutes and turns red when the service is down.

(Replace the placeholder in `.github/workflows/status.yml` only if you prefer a
hardcoded URL; the variable is the supported path.)

## 6. Update the README (post-deploy, ask the agent)

- Put the live URL above the fold, before the badges.
- Note that the instance runs in offline-narrator / BYO-key mode by default.

## 7. Rollback

```bash
fly releases         # list deploys with ids
fly rollback <id>    # back to a known-good release (volumes untouched)
```

## 8. Railway alternative

Equivalent with `railway up`: add the repo, set the same env vars
(`APP_PASSWORD`, `FINSIGHT_API_KEY`, `FINSIGHT_PUBLIC=1`,
`FINSIGHT_API_URL=http://localhost:8000` for same-service calls, plus
`FINSIGHT_RATE_LIMIT_PER_MIN=600` and `FINSIGHT_CORS_ORIGINS=<app-origin>`
on the API service), provision two services from the same Dockerfile (web =
`streamlit run app/Home.py`, api = `uvicorn finance_agent.api:app`), and
attach persistent volumes at `/app/data` + `/app/model_bench`. The
entrypoint's idempotent bootstrap and fingerprint guard behave identically.

## 9. TLS / CSP at the edge (self-hosted only)

Fly's built-in proxy terminates TLS for you; nothing to do on the Fly path. If
instead you self-host behind your own reverse proxy (VPS / home server), use
the ready-made templates instead of starting from scratch — they set CSP,
HSTS, and baseline security headers with the Streamlit/Swagger caveats already
worked out (security audit §4):

- [`deploy/Caddyfile.example`](deploy/Caddyfile.example) — Caddy (TLS
  automatic via Let's Encrypt).
- [`deploy/nginx.conf.example`](deploy/nginx.conf.example) — nginx (certbot
  fills the certificate paths).

Both proxy the Streamlit UI (:8501) and the facts API (:8000). Note the
per-endpoint CSP differences in the files: Swagger UI at `/docs` needs inline
scripts, so the API block is looser than the app block by design.

## 10. Post-deploy checklist

- [ ] `curl https://<app>.fly.dev:8000/api/v1/health` → `{"status":"ok", ...}` under 5 s cold.
- [ ] Dashboard loads in the browser without an API key (offline narrator mode).
- [ ] Ask-the-Agent page shows the BYO-key banner.
- [ ] `STATUS_URL` variable set; first `status/health.log` lines appear.
- [ ] `make verify-secrets` passes against the live URL (401 without key, 200
      with it, app reachable).
- [ ] `FINSIGHT_BUNDLE_KEY` set; the deployed bundle was re-signed on the
      target (a demo-key-signed bundle would refuse to boot — audit §2).
- [ ] README live link added (ask the agent; it also re-wires any docs).
