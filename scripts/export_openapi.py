#!/usr/bin/env python3
"""Export the FastAPI OpenAPI schema to a versioned, committed artifact (E.2).

    python scripts/export_openapi.py [--out docs/technical/openapi.v1.json]

FastAPI already generates the schema from the route signatures, so this is a
thin freeze step: the committed `docs/technical/openapi.v1.json` becomes the
stable, externally-consumable contract (feed it to an SDK generator or a
contract fuzzer) and the docs-consistency gate can diff it against the live
schema to catch accidental contract drift.

No data artifacts are needed — building the app does not load the ledger.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_OUT = ROOT / "docs" / "technical" / "openapi.v1.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    from finance_agent.api import create_app

    schema = create_app().openapi()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    n_paths = len(schema.get("paths", {}))
    print(
        f"OpenAPI schema written: {out} ({n_paths} paths, {len(schema.get('components', {}).get('schemas', {}))} schemas)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
