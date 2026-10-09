"""HMAC-SHA256 signing/verification for serialized model bundles (C.2.4).

``joblib.load`` on an untrusted pickle executes arbitrary code. The training
pipeline therefore writes a ``<bundle>.sig`` file (HMAC-SHA256 of the bundle
bytes) next to every bundle it produces, and ``finance_agent/tools.py`` refuses
to deserialize a bundle whose signature doesn't match — closing the
pickle-RCE gap against a tampered or swapped ``risk_model_bundle.joblib``.

Key: the ``FINSIGHT_BUNDLE_KEY`` env var. When unset, a well-known *demo*
default is used so the protection is on by default for the shipped artifacts.
That default is public, so it only stops accidental corruption or casual
tampering — a real deployment MUST set ``FINSIGHT_BUNDLE_KEY``. Once the env
key is set, unsigned bundles are refused too, so the guarantee can never be
silently downgraded by a missing signature file.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from pathlib import Path

log = logging.getLogger("finance_agent.bundle_security")

# Demo-only default key. Real deployments MUST set FINSIGHT_BUNDLE_KEY — the
# demo key is public, so it only stops accidental corruption / casual
# tampering, not an adversary with repo access. See docs/KNOWN_LIMITATIONS.md.
DEFAULT_DEMO_KEY = "finsight-demo-bundle-key-2026-change-me"


class BundleSignatureError(RuntimeError):
    """Startup-fatal bundle verification failure (audit §2).

    Raised by :func:`ensure_bundle_verified` when ``FINSIGHT_BUNDLE_KEY`` is
    set (production mode) and the configured bundle's signature is missing or
    doesn't verify against that key — i.e. the bundle was signed with a
    *different* key than the deployment is configured with. This is a
    deployment misconfiguration that rule-only fallback would silently mask,
    so it aborts startup instead.
    """


def ensure_bundle_verified(bundle_path: str) -> str:
    """Fail-fast startup guard for the configured model bundle (audit §2).

    - bundle verifies            -> returns the reason ("signature OK").
    - verification fails AND     -> raises :class:`BundleSignatureError` with a
      ``FINSIGHT_BUNDLE_KEY`` is     message pointing at the fix (re-sign on the
      set (production)               deployment target with the real key).
    - verification fails AND     -> returns the reason; the caller keeps the
      env key unset (demo/dev)       documented degrade-to-rule-only behavior.

    The demo default key is public knowledge (it ships in this repo), so a
    mismatch while it is in use only ever indicates local corruption — the
    rule-only fallback is the right response there. Once a real key is set,
    a mismatch means the wrong bundle was deployed or it was signed elsewhere;
    continuing to serve rule-only scores would hide that, so we refuse loudly.
    """
    ok, reason = verify_bundle(bundle_path)
    if ok:
        return reason
    if os.environ.get("FINSIGHT_BUNDLE_KEY", "").strip():
        raise BundleSignatureError(
            f"Model bundle {bundle_path!r} failed signature verification: {reason}. "
            "FINSIGHT_BUNDLE_KEY is set, so an unsigned or mis-signed bundle will NOT be "
            "loaded. Fix: re-sign on the deployment target with the real key "
            "(`FINSIGHT_BUNDLE_KEY=<key> make train`), or set FINSIGHT_BUNDLE_KEY to the "
            "key the bundle was actually signed with — see DEPLOY.md 'Set secrets' and "
            "docs/KNOWN_LIMITATIONS.md section 4."
        )
    return reason


ALGORITHM = "hmac-sha256"


def signing_key() -> str:
    """The current signing key: FINSIGHT_BUNDLE_KEY, else the demo default."""
    return os.environ.get("FINSIGHT_BUNDLE_KEY", "").strip() or DEFAULT_DEMO_KEY


def key_origin() -> str:
    return "env" if os.environ.get("FINSIGHT_BUNDLE_KEY", "").strip() else "demo-default"


def signature_path(bundle_path: str) -> str:
    return f"{bundle_path}.sig"


def digest_for(data: bytes, key: str) -> str:
    """Hex HMAC-SHA256 of `data` under `key`."""
    return hmac.new(key.encode("utf-8"), data, hashlib.sha256).hexdigest()


def write_signature(bundle_path: str) -> str:
    """Compute + persist ``<bundle>.sig``; returns the hex digest (provenance)."""
    data = Path(bundle_path).read_bytes()
    sig = digest_for(data, signing_key())
    Path(signature_path(bundle_path)).write_text(sig + "\n", encoding="utf-8")
    return sig


def verify_bundle(bundle_path: str) -> tuple[bool, str]:
    """Verify ``<bundle>.sig`` against the current key.

    Returns ``(ok, reason)``:

    * no signature file + env key unset  -> ``(True, ...)`` legacy/demo bundle
      (loaded with a warning);
    * no signature file + env key set    -> ``(False, ...)`` refused — the key
      author is declaring that unsigned bundles must not load;
    * signature mismatch                 -> ``(False, ...)`` always refused;
    * signature match                    -> ``(True, ...)``.
    """
    sig_file = Path(signature_path(bundle_path))
    key = signing_key()
    if not sig_file.exists():
        if os.environ.get("FINSIGHT_BUNDLE_KEY", "").strip():
            return (
                False,
                "no signature file and FINSIGHT_BUNDLE_KEY is set — refusing unsigned bundle",
            )
        return True, "unsigned bundle (no signature file; env key unset) — demo/legacy mode"
    try:
        stored = sig_file.read_text(encoding="utf-8").strip()
        actual = digest_for(Path(bundle_path).read_bytes(), key)
    except OSError as exc:
        return False, f"could not read bundle or signature: {exc}"
    if not hmac.compare_digest(stored, actual):
        return False, "signature mismatch — bundle tampered or signed with a different key"
    return True, "signature OK"
