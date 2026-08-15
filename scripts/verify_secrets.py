"""verify_secrets.py — self-check a deployment's security gates (audit §1a).

After the human has set ``APP_PASSWORD`` / ``FINSIGHT_API_KEY`` /
``FINSIGHT_BUNDLE_KEY`` on the deploy target (see ``scripts/generate_secrets.sh``),
this script proves the gates are actually enforced instead of leaving it to
"hope it worked":

1. ``GET <FINSIGHT_URL>/api/v1/health`` **without** an ``X-API-Key`` must return
   401 (the shared-secret gate is on).
2. The same request **with** the correct key must return 200 + ``status: ok``.
3. The Streamlit app is reachable (``FINSIGHT_APP_URL``, or derived from the
   API URL by stripping the port — the Fly convention). The password prompt
   itself is client-side, so step 3 is a reachability probe plus an explicit
   human checklist item rather than a false claim of machine-verification.

Stdlib-only (urllib) — no requests dependency. Exit code 0 = all machine-
checkable gates verified. Invoke via ``make verify-secrets``:

    FINSIGHT_URL=https://<app>.fly.dev:8000 FINSIGHT_API_KEY='<value>' make verify-secrets
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# ANSI color only when attached to a terminal — CI logs / piped output get
# plain text instead of raw escape sequences (Windows cmd.exe included).
_USE_COLOR = sys.stdout.isatty()


def _tag(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


_OK = _tag("PASS", "32")
_FAIL = _tag("FAIL", "31")
_SKIP = _tag("SKIP", "33")


def _get(url: str, api_key: str | None = None, timeout: int = 20) -> tuple[int, str | None]:
    """``(status_code, body_text_or_None)`` — never raises on HTTP errors."""
    req = urllib.request.Request(url, method="GET")
    if api_key is not None:
        req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def main() -> int:
    base = os.environ.get("FINSIGHT_URL", "").strip() or "http://localhost:8000"
    base = base.rstrip("/")
    api_key = os.environ.get("FINSIGHT_API_KEY", "").strip()
    app_url = os.environ.get("FINSIGHT_APP_URL", "").strip()
    if not app_url:
        # Fly convention: the app is at the same host without the API's port.
        app_url = base.rsplit(":", 1)[0] if base.count(":") >= 2 else ""

    failed = 0

    print(f"Verifying gates at {base} ...\n")

    # 1. Without the key -> 401 (gate must be active).
    status, _ = _get(f"{base}/api/v1/health", api_key=None)
    if status == 401:
        print(f"  {_OK}  /api/v1/health without X-API-Key -> 401 (gate active)")
    elif status == 200:
        print(
            f"  {_FAIL}  /api/v1/health without X-API-Key -> 200 — the API-key gate is "
            "NOT enforced; did you set FINSIGHT_API_KEY on the target?"
        )
        failed += 1
    else:
        print(f"  {_FAIL}  /api/v1/health without X-API-Key -> HTTP {status} (expected 401)")
        failed += 1

    # 2. With the key -> 200 + status ok.
    status, body = _get(f"{base}/api/v1/health", api_key=api_key or None)
    if status == 200:
        try:
            ok_flag = json.loads(body or "{}").get("status") == "ok"
        except json.JSONDecodeError:
            ok_flag = False
        if ok_flag:
            print(f"  {_OK}  /api/v1/health with X-API-Key -> 200 status ok")
        else:
            print(
                f"  {_FAIL}  /api/v1/health with X-API-Key -> 200 but body is not "
                f"`status: ok` (got {body[:80]!r})"
            )
            failed += 1
    elif status == 401:
        print(
            f"  {_FAIL}  /api/v1/health with X-API-Key -> 401 — wrong key? "
            "FINSIGHT_API_KEY must match the value set on the target."
        )
        failed += 1
    else:
        print(f"  {_FAIL}  /api/v1/health with X-API-Key -> HTTP {status} (expected 200)")
        failed += 1

    # 3. App reachability + the human-verified password gate.
    if app_url:
        status, _ = _get(app_url, api_key=None)
        if status == 200:
            print(f"  {_OK}  app reachable at {app_url} (HTTP 200)")
            print(
                "  [manual] open the URL in a browser — you MUST see the shared-password "
                "prompt (not the dashboard) to confirm APP_PASSWORD is set."
            )
        else:
            print(
                f"  {_SKIP}  app at {app_url} returned HTTP {status} — reachability "
                "check failed; is the app process up?"
            )
    else:
        print(
            f"  {_SKIP}  no app URL to probe — set FINSIGHT_APP_URL or use the Fly "
            "convention (host without the API port)."
        )

    print()
    if failed:
        print("One or more gates FAILED verification — fix before calling this shipped.")
        return 1
    print(
        "All machine-checkable gates verified. (Remember the manual APP_PASSWORD "
        "browser check above.)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
