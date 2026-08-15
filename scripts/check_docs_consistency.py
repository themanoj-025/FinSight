#!/usr/bin/env python3
"""Docs-code consistency check (Phase 0.4 / definition-of-done).

Confirms that the claims docs/*.md make about the data schema, the test
suite, and the security controls match the actual code — the project's rule
is "docs must never claim something that doesn't exist". Exits non-zero on
any drift so CI and a pre-release `make docs-check` fail loudly instead of
shipping a stale document.

    python scripts/check_docs_consistency.py

Checks:
  1. docs/technical/Schema.md §2 — CSV columns == generate_data.generate() output
  2. docs/technical/Schema.md §4 — feature matrix columns == build_features() output
  3. docs/technical/Testing.md §3 — every TC-xxx row names a real test file and
     every backticked test_* function actually exists in tests/
  4. README.md — every local link target exists; no placeholder text
  5. docs/technical/SecurityAndCompliance.md — every "(implemented)" control
     maps to a real file/function (auth gate, config validation, key
     validation, system prompt, joblib note, pip-audit in CI)
  6. docs/KNOWN_LIMITATIONS.md exists and is non-trivial
  7. Tech-stack guardrail — every technology named in a tech-stack table in
     PROJECT_OVERVIEW.md / README.md must be real: imported by code under
     finance_agent/ or model_bench/, declared in pyproject.toml, or on an
     explicit platform/stdlib allowlist. This makes the "no phantom
     technology" rule (MLflow/Optuna incident, Phase 0.1) permanent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # allow running from any cwd

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record and print one check; ASCII separators keep Windows consoles clean."""
    RESULTS.append((name, ok))
    suffix = f" - {detail}" if detail else ""
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{suffix}")


def section(md: str, title: str) -> str:
    """Return the body of the '## <title>' section (or '' if missing)."""
    parts = re.split(r"^## ", md, flags=re.M)
    for part in parts:
        if part.startswith(title):
            return part
    return ""


def backticked(text: str) -> list[str]:
    return re.findall(r"`([^`]+)`", text)


def main() -> int:
    print("FinSight Agent - docs-code consistency check\n")

    # ------------------------------------------------------------- 1. schema
    schema = (ROOT / "docs/technical/Schema.md").read_text(encoding="utf-8")
    doc_cols = {
        m.group(1)
        for line in section(schema, "2. Column Reference").splitlines()
        if (m := re.match(r"^\|\s*`([a-zA-Z_][a-zA-Z0-9_]*)`\s*\|", line))
    }
    from generate_data import generate

    sample = generate(days=2, seed=1, n_background_accounts=2)
    actual_cols = set(sample.columns)
    missing = sorted(doc_cols - actual_cols)
    extra = sorted(actual_cols - doc_cols)
    check(
        "Schema.md CSV columns match generate_data.generate()",
        not missing and not extra,
        f"documented-but-absent: {missing or 'none'}; code-but-undocumented: {extra or 'none'}",
    )

    # ------------------------------------------------------------- 2. features
    from finance_agent.features import build_features

    # Only table rows list features; the section intro also contains a code
    # reference like `finance_agent/features.py::build_features(df)` which is
    # not a feature name. Filter to bare snake_case identifiers.
    feat_table = "\n".join(
        line for line in section(schema, "4. Feature Matrix").splitlines() if line.startswith("|")
    )
    doc_feats = {t for t in backticked(feat_table) if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", t)}
    actual_feats = set(build_features(sample).columns)
    f_missing = sorted(doc_feats - actual_feats)
    f_extra = sorted(actual_feats - doc_feats)
    check(
        "Schema.md feature matrix matches build_features()",
        not f_missing and not f_extra,
        f"documented-but-absent: {f_missing or 'none'}; code-but-undocumented: {f_extra or 'none'}",
    )

    # --------------------------------------------------------------- 3. tests
    testing = (ROOT / "docs/technical/Testing.md").read_text(encoding="utf-8")
    rows = [
        (m.group(1), m.group(2), m.group(3))
        for line in section(testing, "3. Critical Test Cases").splitlines()
        if (m := re.match(r"^\|\s*(TC-\d+)\s*\|\s*([\w.]+\.py)\s*\|(.*)$", line))
    ]
    check("Testing.md lists test cases", bool(rows), f"{len(rows)} TC rows")
    bad_rows: list[str] = []
    for tc, fname, rest in rows:
        path = ROOT / "tests" / fname
        if not path.exists():
            bad_rows.append(f"{tc}: tests/{fname} does not exist")
            continue
        src = path.read_text(encoding="utf-8")
        for token in backticked(rest):
            if token.startswith("test_") and f"def {token}(" not in src:
                bad_rows.append(f"{tc}: tests/{fname} has no def {token}()")
    check("Every TC row names a real test file/function", not bad_rows, "; ".join(bad_rows))

    # -------------------------------------------------------------- 4. readme
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    broken_links: list[str] = []
    for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", readme):
        target = m.group(1).split("#")[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (ROOT / target).exists():
            broken_links.append(target)
    check("README local links resolve", not broken_links, "; ".join(broken_links) or "all resolve")

    placeholders = [
        t
        for t in ("YOUR_USERNAME", "docs/img/", "example.com", "lorem", "TBD", "FIXME")
        if t in readme
    ]
    check("README has no placeholder text", not placeholders, "; ".join(placeholders) or "clean")

    # ------------------------------------------------------------- 5. security
    def src_has(rel: str, needle: str) -> bool:
        return needle in (ROOT / rel).read_text(encoding="utf-8")

    claims = [
        ("common.py gates pages with require_auth", "app/common.py", "def require_auth"),
        (
            "config_schema.py validates config",
            "finance_agent/config_schema.py",
            "class ConfigError",
        ),
        ("agent.py ships a system prompt", "finance_agent/agent.py", "SYSTEM_PROMPT ="),
        ("agent.py validates API keys", "finance_agent/agent.py", "def validate_api_key"),
        ("tools.py documents the joblib pickle risk", "finance_agent/tools.py", "untrusted"),
        ("api.py implements the X-API-Key gate", "finance_agent/api.py", "X-API-Key"),
        ("CI runs pip-audit", ".github/workflows/ci.yml", "pip-audit"),
        (
            "CI runs the slow suite separately",
            ".github/workflows/ci.yml",
            '-m "slow and not data_realism"',
        ),
    ]
    for label, rel, needle in claims:
        check(f"Security claim — {label}", src_has(rel, needle), rel)

    # ---------------------------------------------------------- 6. limitations
    lim = ROOT / "docs/KNOWN_LIMITATIONS.md"
    substantial = lim.exists() and len(lim.read_text(encoding="utf-8")) > 1500
    check(
        "docs/KNOWN_LIMITATIONS.md exists and is substantive",
        substantial,
        "missing or too short" if not substantial else "ok",
    )

    # --------------------------------------------------- 7. tech-stack guardrail
    # Every technology in a tech-stack table must be real: imported under
    # finance_agent/ or model_bench/, declared in pyproject.toml, or on the
    # platform/stdlib allowlist. This is the permanent version of the Phase 0.1
    # fix that removed the phantom MLflow/Optuna/FAISS rows.

    # Display name -> importable module name (for the import/dep checks).
    TECH_MODULES = {
        "Python": None,
        "scikit-learn": "sklearn",
        "LightGBM": "lightgbm",
        "Streamlit": "streamlit",
        "FastAPI": "fastapi",
        "Anthropic Claude": "anthropic",
        "FAISS": "faiss",
        "PyYAML": "yaml",
        "pytest": "pytest",
        "ruff": "ruff",
        "mypy": "mypy",
        "GitHub Actions": None,
        "CI/CD": None,
        "SQLite": None,
    }
    # Platforms / stdlib / non-Python items that are legitimately not imports.
    TECH_ALLOWLIST = {"python", "sqlite", "github actions", "ci/cd"}

    _src_all = ""
    for _rel in ["finance_agent", "model_bench"]:
        for _p in sorted((ROOT / _rel).rglob("*.py")):
            _src_all += _p.read_text(encoding="utf-8") + "\n"

    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    def _tech_ok(tech: str) -> tuple[bool, str]:
        """A technology is real if it is imported, declared, or allowed."""
        norm = tech.strip().strip("`").lower()
        # Strip parentheticals like "FAISS (optional)" -> "FAISS".
        base = tech.strip().strip("`").split("(")[0].strip()
        if norm in TECH_ALLOWLIST:
            return True, "platform/stdlib allowlist"
        mod = TECH_MODULES.get(base, base.lower().replace(" ", "_"))
        if mod is None:
            return True, "allowlist"
        if re.search(rf"(?:^|\n)\s*(?:import {mod}\b|from {mod}\b)", _src_all):
            return True, "imported in finance_agent//model_bench/"
        if re.search(rf"\b{re.escape(mod)}\b", pyproject_text):
            return True, "declared in pyproject.toml"
        return False, "no import under finance_agent//model_bench/ and not in pyproject.toml"

    def _techs_from_md(md: str, doc_name: str) -> list[tuple[str, str]]:
        """Yield (technology, source-doc) pairs from tech-stack-style tables.

        A tech-stack table is recognized by its header row (first cell a layer
        kind like ``Layer``/``Library``, second cell ``Technology``/``Stack``);
        every following table row contributes its second cell as a technology.
        """
        out: list[tuple[str, str]] = []
        in_table = False
        for line in md.splitlines():
            if not line.strip().startswith("|"):
                in_table = False
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            if cells[1].lower() in {"technology", "stack"} and cells[0].lower() in {
                "layer",
                "technology",
                "library",
                "framework",
                "stack",
            }:
                in_table = True
                continue
            if in_table:
                tech = cells[1].strip().strip("`")
                if tech and not tech.startswith("---"):
                    out.append((tech, doc_name))
        return out

    techs = _techs_from_md(
        (ROOT / "PROJECT_OVERVIEW.md").read_text(encoding="utf-8"), "PROJECT_OVERVIEW.md"
    ) + _techs_from_md(readme, "README.md")
    bad_techs: list[str] = []
    seen: set[str] = set()
    for tech, doc in techs:
        key = tech.lower()
        if key in seen:
            continue
        seen.add(key)
        ok, reason = _tech_ok(tech)
        if not ok:
            bad_techs.append(f"{tech} ({doc} — {reason})")
    check(
        "Tech-stack tables claim only real technologies",
        not bad_techs,
        "; ".join(bad_techs) or f"{len(seen)} technologies verified",
    )

    # --------------------------------------------------------------- summary
    failed = [name for name, ok in RESULTS if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"{len(failed)} of {len(RESULTS)} checks FAILED: {', '.join(failed)}")
        print("Fix the drift before tagging a release.")
        return 1
    print(f"All {len(RESULTS)} docs-code consistency checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
