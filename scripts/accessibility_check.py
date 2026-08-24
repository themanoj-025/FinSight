#!/usr/bin/env python3
"""Accessibility + mobile render check for the Streamlit app (Phase F.4).

    python scripts/accessibility_check.py                # boot Streamlit, check, exit
    python scripts/accessibility_check.py --attach http://127.0.0.1:8501

Boots (or attaches to) a Streamlit instance, loads every page in
``app/pages/`` plus Home, and runs axe-core against each:

  * **Hard fail** on any axe violation of severity ``critical`` or ``serious``
    (WCAG 2.x A/AA). Minor/moderate violations are reported but do not fail —
    Streamlit's own widgets carry a handful of known minor issues that are out
    of our theme's control.
  * **Mobile render check** (375px viewport, the two layout-dense pages —
    Dashboard and Transactions): a page that horizontally overflows its
    viewport fails.
  * Any page that renders a Streamlit exception box (``stException``) fails —
    a rendered app with a red error is not accessible by construction.

Requires the optional ``a11y`` extra (``pip install -e ".[a11y]"`` plus
``playwright install chromium``). axe-core is injected from the jsDelivr CDN
at check time so no vendored JS ships in the repo.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES_DIR = ROOT / "app" / "pages"
AXE_URL = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"
HARD_SEVERITIES = {"critical", "serious"}
MOBILE_W = 375
REPORT = ROOT / "a11y" / "report.txt"

# Streamlit 1.6x renders the ROOT page's sidebar as a role-less
# `<section data-testid="stSidebar" aria-expanded="true">`. `aria-expanded`
# is only valid on interactive roles, so axe flags it `aria-allowed-attr`
# (critical) — but the markup is framework-generated and cannot be patched
# from app code (st.markdown/st.html cannot execute JS in the main document),
# and it only appears on the root view (multipage subpages don't emit the
# attribute). Scoped exclusion: an `aria-allowed-attr` violation is ignored
# only when every flagged node is inside the sidebar element, so the gate
# stays strict for everything under our control. The report notes the
# exclusion so the pass is transparent, not silent.
_STREAMLIT_SIDEBAR_ARIA = (
    "aria-allowed-attr",
    "stSidebar",  # axe reports the target by class (`.stSidebar`)
)


def _is_framework_sidebar_attr(violation: dict) -> bool:
    """True if `violation` is the known Streamlit sidebar aria-expanded quirk."""
    rule, marker = _STREAMLIT_SIDEBAR_ARIA
    if violation.get("id") != rule:
        return False
    nodes = violation.get("nodes", [])
    return bool(nodes) and all(
        any(marker in (target or "") for target in node.get("target", [])) for node in nodes
    )


def _wait_healthy(base: str, port: int, proc: subprocess.Popen[bytes] | None) -> None:
    for _ in range(90):
        try:
            with urllib.request.urlopen(f"{base}/_stcore/health", timeout=2) as r:
                if r.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        if proc is not None and proc.poll() is not None:
            raise SystemExit("Streamlit exited before becoming healthy (see stderr log)")
        time.sleep(1)
    raise SystemExit(f"Streamlit never became healthy at {base}/_stcore/health")


def _page_urls(base: str) -> list[tuple[str, str]]:
    """(label, url) for Home + every multipage file (Streamlit 1.36+ paths)."""
    urls = [("Home", base + "/")]
    for page in sorted(PAGES_DIR.glob("*.py")):
        label = page.stem
        urls.append((label, f"{base}/{label}"))
    return urls


def _wait_rendered(page) -> None:
    """Wait for the Streamlit app shell and for the run spinner to clear."""
    page.wait_for_selector('[data-testid="stAppViewContainer"]', timeout=30_000)
    # The spinner is present while the script rerun is in flight; its removal
    # means the page finished rendering (charts/dframes included).
    for _ in range(120):
        spinner = page.query_selector('[data-testid="stStatusWidget"], [data-testid="stSpinner"]')
        if spinner is None:
            time.sleep(0.5)
            # Give the DOM a beat to settle after the spinner clears.
            return
        time.sleep(0.5)
    raise SystemExit("page never finished rendering (spinner did not clear)")


def _axe_scan(page) -> list[dict]:
    page.add_script_tag(url=AXE_URL)
    return page.evaluate(
        """() => new Promise((resolve) => {
            axe.run(document, { resultTypes: ["violations"] })
              .then(r => resolve(r.violations))
              .catch(err => resolve([{ id: 'axe-run-error',
                  impact: 'critical',
                  nodes: [{ target: ['<runner>'], failureSummary: String(err) }] }]));
        })"""
    )


def _check_page(page, label: str, url: str, lines: list[str]) -> int:
    fails = 0
    page.goto(url, wait_until="domcontentloaded")
    _wait_rendered(page)

    # 1. Rendered exception box => not accessible by construction.
    if page.query_selector('[data-testid="stException"]'):
        lines.append(f"  FAIL  {label:22s} page rendered a Streamlit exception")
        return 1

    # 2. axe-core scan.
    violations = _axe_scan(page)
    framework_excluded = [v for v in violations if _is_framework_sidebar_attr(v)]
    hard = [
        v
        for v in violations
        if v.get("impact") in HARD_SEVERITIES and not _is_framework_sidebar_attr(v)
    ]
    if hard:
        fails += 1
        for v in hard:
            lines.append(
                f"  FAIL  {label:22s} axe {v.get('impact')}: {v.get('id')} "
                f"({len(v.get('nodes', []))} node(s))"
            )
    else:
        lines.append(
            f"  PASS  {label:22s} axe: no critical/serious violations "
            f"({len(violations) - len(framework_excluded)} minor/moderate total"
            + (
                f" + {len(framework_excluded)} excluded Streamlit-sidebar quirk)"
                if framework_excluded
                else ")"
            )
        )
    if framework_excluded:
        lines.append(
            f"  NOTE  {label:22s} excluded {_STREAMLIT_SIDEBAR_ARIA[0]} on "
            f"`{_STREAMLIT_SIDEBAR_ARIA[1]}` — Streamlit-framework markup, not app code "
            f"(see the docstring)"
        )

    # 3. Mobile overflow on the two layout-dense pages.
    if label in {"1_Dashboard", "2_Transactions"}:
        page.set_viewport_size({"width": MOBILE_W, "height": 812})
        page.goto(url, wait_until="domcontentloaded")
        _wait_rendered(page)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth > window.innerWidth + 1"
        )
        if overflow:
            fails += 1
            lines.append(f"  FAIL  {label:22s} horizontal overflow at {MOBILE_W}px viewport")
        else:
            lines.append(f"  PASS  {label:22s} no horizontal overflow at {MOBILE_W}px")

    return fails


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attach", help="Check an already-running Streamlit at this URL")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            'playwright is not installed — run `pip install -e ".[a11y]"` and '
            "`python -m playwright install chromium` first."
        ) from None

    proc: subprocess.Popen[bytes] | None = None
    if args.attach:
        base = args.attach.rstrip("/")
    else:
        base = f"http://127.0.0.1:{args.port}"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                "app/Home.py",
                "--server.headless",
                "true",
                "--server.port",
                str(args.port),
                "--browser.gatherUsageStats",
                "false",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    _wait_healthy(base, args.port, proc)

    lines: list[str] = [
        "FinSight Agent — accessibility + mobile render check (F.4)",
        f"  axe-core: {AXE_URL}",
        f"  hard-fail severities: {', '.join(sorted(HARD_SEVERITIES))}",
        "",
    ]
    fails = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            for label, url in _page_urls(base):
                fails += _check_page(page, label, url, lines)
            browser.close()
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    lines.append("")
    ok = fails == 0
    lines.append(
        "RESULT: "
        + (
            "PASS — all pages clean (axe critical/serious + mobile layout)"
            if ok
            else f"FAIL — {fails} check(s) failed"
        )
    )
    print("\n".join(lines))

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  (report written to {REPORT})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
