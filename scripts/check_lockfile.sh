#!/bin/sh
# check_lockfile.sh — verify requirements.lock matches pyproject.toml.
#
# Shared by CI (.github/workflows/ci.yml, lockfile job) and the pre-push git
# hook (.githooks/pre-push), so the two checks can never drift apart.
#
# Why a byte-identical diff: the lockfile is recompiled in a temp dir with the
# SAME relative output filename, so uv's header comment is identical too — a
# stale lockfile (or a pyproject edit) shows up as a real diff, not noise.
#
# Usage:
#   scripts/check_lockfile.sh                       # warn (exit 0) if uv is missing
#   scripts/check_lockfile.sh --require-uv          # fail if uv is missing (CI)
#   scripts/check_lockfile.sh --python-version=3.10 # target Python (default 3.10)

set -u

require_uv=0
python_version="3.10"

for arg in "$@"; do
  case "$arg" in
    --require-uv) require_uv=1 ;;
    --python-version=*) python_version="${arg#*=}" ;;
    -h | --help)
      echo "usage: check_lockfile.sh [--require-uv] [--python-version=X]"
      exit 0
      ;;
    *)
      echo "check_lockfile.sh: unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT" || exit 1

fail() {
  echo "error: $*" >&2
  [ "${GITHUB_ACTIONS:-}" = "true" ] && echo "::error::$*" >&2
  exit 1
}
notice() {
  echo "$*"
  [ "${GITHUB_ACTIONS:-}" = "true" ] && echo "::notice::$*"
}

if ! command -v uv >/dev/null 2>&1; then
  if [ "$require_uv" = 1 ]; then
    fail "uv not found — install it (CI pins uv==0.12.2)"
  fi
  echo "warning: uv not found; skipping lockfile drift check (CI's lockfile job is the authoritative gate)" >&2
  exit 0
fi

# The lockfile was generated with uv 0.12.2, whose compiled output (header,
# comments, ordering) is stable within a version. It is compiled with
# `--python-platform linux` so the committed lockfile is platform-neutral:
# Windows-only deps (colorama via click) never leak into it, and a dev
# recompiling on Windows gets the same bytes CI's Linux recompile produces.
# (The CI `lockfile` job and the pre-push hook share this script, so the two
# checks can never drift apart.) A different uv can produce a
# different-but-valid file, which would read as false drift here and, worse,
# tempt a contributor to regenerate with the wrong uv — creating real drift
# that pinned CI then rejects. Warn on mismatch; never fail on it.
uv_version="$(uv --version 2>/dev/null | awk '{print $2}')"
case "$uv_version" in
  0.12.*) : ;;
  "")
    echo "warning: could not read uv version; proceeding (CI pins uv==0.12.2)" >&2
    ;;
  *)
    echo "warning: uv $uv_version on PATH differs from the pinned CI version (uv 0.12.2);" >&2
    echo "         compiled output may differ and cause a false drift signal — install the" >&2
    echo "         pinned version with: pip install uv==0.12.2" >&2
    ;;
esac

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cp pyproject.toml "$tmp/"
if ! (cd "$tmp" && uv pip compile pyproject.toml -o requirements.lock --python-version "$python_version" --python-platform linux --quiet 2>"$tmp/uv.err"); then
  if [ "$require_uv" = 1 ]; then
    echo "error: uv pip compile failed:" >&2
    cat "$tmp/uv.err" >&2
    exit 1
  fi
  # A compile failure (e.g. transient index outage) is NOT evidence of drift —
  # only an actual diff is. In hook mode, warn and let the push through; CI's
  # lockfile job is the authoritative gate.
  echo "warning: could not verify the lockfile (uv compile failed — offline/index issue?):" >&2
  cat "$tmp/uv.err" >&2
  echo "         treating as unverifiable; CI's lockfile job will still catch real drift." >&2
  exit 0
fi

if ! diff -u requirements.lock "$tmp/requirements.lock"; then
  echo "error: requirements.lock is out of sync with pyproject.toml" >&2
  echo "regenerate it with: uv pip compile pyproject.toml -o requirements.lock --python-version $python_version --python-platform linux" >&2
  [ "${GITHUB_ACTIONS:-}" = "true" ] && echo "::error::requirements.lock is out of sync with pyproject.toml" >&2
  exit 1
fi

count="$(grep -c '==' requirements.lock || true)"
notice "requirements.lock is in sync ($count pinned packages)"
exit 0
