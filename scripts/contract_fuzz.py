#!/usr/bin/env python3
"""Run schemathesis contract fuzzing against a live local API (Phase F.3).

    python scripts/contract_fuzz.py                 # boot a throwaway API, fuzz, exit
    python scripts/contract_fuzz.py --attach http://127.0.0.1:8000   # fuzz an already-running API

Fuzzes every operation in the committed ``docs/technical/openapi.v1.json``
against a real API instance, failing on 500s and responses that violate the
documented schema (i.e. contract drift between the API and its published
schema). Used by both the nightly ``contract-fuzz`` CI job and
``make contract-fuzz`` so the two never drift apart.

Scope-down (deliberate): ``POST /api/v1/reload`` is excluded via
``--exclude-path`` — a fuzzer must not repeatedly force the API to drop its
facts snapshot (state mutation, not a read). See the CI job comment for the
same rationale.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

EXCLUDE_PATHS = ("/api/v1/reload",)


def _boot(port: int) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "finance_agent.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    proc.terminate()
    raise SystemExit(f"API never became healthy on port {port}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default="docs/technical/openapi.v1.json")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument(
        "--attach", help="Fuzz an already-running API at this URL instead of booting one"
    )
    parser.add_argument("--max-examples", type=int, default=5)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    base = args.attach or f"http://127.0.0.1:{args.port}"
    proc = None if args.attach else _boot(args.port)
    try:
        # `python -m schemathesis` does NOT work — the schemathesis 4.x package
        # has no __main__ ("No module named schemathesis.__main__"). The CLI is
        # the `schemathesis` console script installed by the `contract` extra
        # (pip install -e ".[dev,contract]"), so resolve it from PATH.
        exe = shutil.which("schemathesis")
        if exe is None:
            raise SystemExit(
                "schemathesis CLI not found on PATH — install the [contract] extra: "
                "pip install -e '.[dev,contract]'"
            )
        cmd = [exe, "run", args.schema, "--url", base]
        for path in EXCLUDE_PATHS:
            cmd += ["--exclude-path", path]
        cmd += ["--max-examples", str(args.max_examples), "--seed", str(args.seed), "--no-shrink"]
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            print("::error::schemathesis found contract violations (500s or schema drift)")
        return rc
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
