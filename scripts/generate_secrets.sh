#!/bin/sh
# generate_secrets.sh — generate strong, unique values for the deployment secrets.
#
# Security-audit runbook (§1a): the deploying human should NOT improvise secret
# values at 11pm. This script mints cryptographically-strong random values and
# prints them for you to copy into your password manager. It deliberately does
# NOT persist them anywhere (no .env write, no commit) — storage is your job.
#
# Usage:
#   ./scripts/generate_secrets.sh            # print all three secrets
#   ./scripts/generate_secrets.sh --json     # JSON for scripting
#
# After you've set the values on the deploy target (e.g. `fly secrets set`),
# verify the gates are actually enforced with:
#   FINSIGHT_URL=https://<app>.fly.dev:8000 FINSIGHT_API_KEY='<value>' make verify-secrets

set -eu

if command -v openssl >/dev/null 2>&1; then
  gen() { openssl rand -base64 24 | tr -d '\n\r'; }   # ~32 bytes; tr strips the CRLF that Windows shells add
  hex() { openssl rand -hex 32; }
elif command -v python >/dev/null 2>&1; then
  # Portable fallback: python's secrets module (no openssl required).
  gen() { python -c 'import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode().rstrip("="))'; }
  hex() { python -c 'import secrets; print(secrets.token_hex(32))'; }
else
  echo "error: need either openssl or python to generate secrets" >&2
  exit 1
fi

APP_PASSWORD="$(gen)"
FINSIGHT_API_KEY="$(hex)"
FINSIGHT_BUNDLE_KEY="$(hex)"

if [ "${1:-}" = "--json" ]; then
  printf '{"APP_PASSWORD": "%s", "FINSIGHT_API_KEY": "%s", "FINSIGHT_BUNDLE_KEY": "%s"}\n' \
    "$APP_PASSWORD" "$FINSIGHT_API_KEY" "$FINSIGHT_BUNDLE_KEY"
  exit 0
fi

echo "Generate three strong secrets (nothing was written to disk):"
echo
echo "  APP_PASSWORD=$APP_PASSWORD"
echo "  FINSIGHT_API_KEY=$FINSIGHT_API_KEY"
echo "  FINSIGHT_BUNDLE_KEY=$FINSIGHT_BUNDLE_KEY"
echo
echo "Store these in your password manager NOW — this script does not persist them."
echo
echo "Next:"
echo "  1. fly secrets set APP_PASSWORD='...' FINSIGHT_API_KEY='...' FINSIGHT_BUNDLE_KEY='...'"
echo "  2. Re-sign the model bundle on the target so it matches the real key:"
echo "       fly ssh console -C \"FINSIGHT_BUNDLE_KEY='...' make train\"   # or the retrain path"
echo "  3. Verify the gates are enforced:"
echo "       FINSIGHT_URL=https://<app>.fly.dev:8000 FINSIGHT_API_KEY='...' make verify-secrets"
