"""Phase A.3 — canary / shadow evaluation for the weekly retrain.

Before the retrain workflow opens its PR, the incumbent production bundle
(the previous commit's ``risk_model_bundle.joblib``) and the freshly trained
candidate are *both* scored on the same temporal holdout window. The output is
a per-archetype recall diff table for the retrain PR body plus a
machine-readable verdict: if any archetype's recall drops by more than
``model_bench.canary_tolerance`` (default 0.05 = 5 percentage points), the
workflow labels the PR ``canary-regression`` and requires explicit human
sign-off instead of a routine merge.

    python model_bench/canary.py \\
        --old /tmp/old_bundle.joblib \\
        --new model_bench/risk_model_bundle.joblib \\
        --data data/transactions.csv \\
        --config config.yaml \\
        --out /tmp/canary_body.md

Prints the verdict token (``REGRESSION`` | ``CLEAN``) on stdout — nothing else
— so the workflow can capture it directly. The markdown body is written to
``--out``. Exit code 2 means the candidate bundle is missing or its feature
schema no longer matches ``build_features`` (a real workflow error, not a
verdict); a missing *incumbent* bundle is expected on the first retrain and
degrades to a no-baseline profile instead of failing.

The temporal split and feature computation replicate ``train_and_compare.py``
exactly, so canary numbers are directly comparable to the benchmark's own
holdout metrics.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finance_agent.features import build_features  # noqa: E402
from model_bench import evaluate, models  # noqa: E402

DEFAULT_TOLERANCE = 0.05


def load_ledger(path: str) -> pd.DataFrame:
    """Load the ledger, keeping the same CSV/Parquet behavior as the benchmark."""
    if str(path).lower().endswith((".parquet", ".pq")):
        df = pd.read_parquet(path, dtype_backend="pyarrow")
        for col in df.columns:
            dt = df[col].dtype
            if isinstance(dt, pd.ArrowDtype):
                nd = dt.numpy_dtype
                if np.issubdtype(nd, np.number) or np.issubdtype(nd, np.bool_):
                    df[col] = df[col].astype(nd)
        return df
    return pd.read_csv(path)


def holdout_window(ledger: pd.DataFrame, test_size: float) -> pd.DataFrame:
    """The exact temporal split ``train_and_compare.py`` uses: sort by ``step``
    (stable, so same-step ties are deterministic across pandas versions) and cut
    at ``(1 - test_size)``. No shuffling — a test row's features only reference
    information at or before its own step."""
    d = ledger.sort_values("step", kind="stable").reset_index(drop=True)
    cut = int(len(d) * (1.0 - test_size))
    return d.iloc[cut:].copy()


def _load_bundle(bundle_path: str) -> dict[str, Any] | None:
    """Load a ``risk_model_bundle.joblib`` dict, or None when there is no
    incumbent to compare (first retrain, or the previous PR never force-added
    a bundle): the workflow expects that and reports a no-baseline profile."""
    if not bundle_path or not os.path.exists(bundle_path):
        return None
    try:
        bundle = joblib.load(bundle_path)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(bundle, dict) or "best_model" not in bundle:
        return None
    return bundle


def _check_feature_alignment(bundle: dict[str, Any], columns: list[str]) -> None:
    """Fail loudly if a bundle's stored feature schema no longer matches the
    current ``build_features`` output. The canary is the gate that decides
    human sign-off, so a schema drift must never silently produce scores on
    misaligned columns (``predict_scores`` maps stored names positionally)."""
    stored = bundle.get("feature_names")
    if stored and list(stored) != list(columns):
        raise RuntimeError(
            "bundle feature schema mismatch: bundle was trained on "
            f"{len(stored)} features, current build_features yields "
            f"{len(columns)}. The canary cannot compare across a feature-schema "
            "change — retrain from a clean checkout instead."
        )


def _score_bundle_dict(bundle: dict[str, Any], X: np.ndarray) -> np.ndarray:
    """Score a feature matrix with an already-loaded bundle dict."""
    Xm = np.asarray(X, dtype=float)
    if bundle.get("needs_scaling") and bundle.get("scaler") is not None:
        Xm = bundle["scaler"].transform(Xm)
    return np.asarray(models.predict_scores(bundle["best_model"], Xm), dtype=float)


def score_bundle(bundle_path: str, X: np.ndarray) -> np.ndarray | None:
    """Score the holdout feature matrix with a bundle dict (see ``_load_bundle``
    for the no-incumbent None contract)."""
    bundle = _load_bundle(bundle_path)
    if bundle is None:
        return None
    return _score_bundle_dict(bundle, X)


def _arch_map(table: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    return {
        str(r["archetype"]): {
            "recall": float(r["recall"]),
            "support": int(r["support"]),
        }
        for r in table.to_dict(orient="records")
    }


def diff_table(
    old_table: pd.DataFrame,
    new_table: pd.DataFrame,
    tolerance: float,
) -> tuple[list[dict[str, object]], list[str]]:
    """Per-archetype old→new recall diff (rows) plus the regressed archetypes.

    A regression is a drop of more than ``tolerance`` in recall units (0.05 =
    5 percentage points). Archetypes that only exist on one side of the
    comparison are shown honestly as ``n/a`` and can never count as a
    regression — there is nothing to compare them against.
    """
    old_by = _arch_map(old_table)
    new_by = _arch_map(new_table)
    rows: list[dict[str, object]] = []
    regressed: list[str] = []
    for arch in sorted(set(old_by) | set(new_by)):
        old = old_by.get(arch)
        new = new_by.get(arch)
        delta_pp: float | None = None
        if old is not None and new is not None:
            # Round to kill binary float noise at the tolerance boundary: a
            # -5 pp drop with a 0.05 tolerance must stay clean, but 0.95 - 1.0
            # is -0.05000000000000004 in IEEE 754 — unrounded it would flip.
            delta_pp = round((float(new["recall"]) - float(old["recall"])) * 100.0, 6)
            if delta_pp < -tolerance * 100.0:
                regressed.append(arch)
        rows.append(
            {
                "archetype": arch,
                "old": old["recall"] if old else None,
                "new": new["recall"] if new else None,
                "delta_pp": delta_pp,
                "support": int((new or old or {"support": 0})["support"]),
            }
        )
    return rows, regressed


def render_body(
    rows: list[dict[str, object]],
    regressed: list[str],
    tolerance: float,
    has_baseline: bool,
) -> str:
    """Markdown section for the retrain PR body: diff table + verdict note."""
    tol_pp = tolerance * 100.0
    if not rows:
        return (
            "> No fraud-archetype rows in the holdout window — nothing to compare. "
            "(Canary verdict: clean.)"
        )

    def _fmt(v: object) -> str:
        if not isinstance(v, (int, float)):
            return "n/a"
        return f"{float(v):.3f}"

    lines = [
        "| Archetype | Incumbent recall @0.5 | Candidate recall @0.5 | Δ (pp) | Support |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        delta = r["delta_pp"]
        if not isinstance(delta, (int, float)):
            delta_s = "n/a"
        else:
            delta_s = f"{float(delta):+.1f}"
            if delta < -tol_pp:
                delta_s = f"**{delta_s}** ⚠️"
        lines.append(
            f"| {r['archetype']} | {_fmt(r['old'])} | {_fmt(r['new'])} | "
            f"{delta_s} | {r['support']} |"
        )
    table = "\n".join(lines)

    if not has_baseline:
        note = (
            "> No incumbent bundle found in git — first retrain since canary evaluation "
            "was added. The table shows the candidate's recall profile with no baseline "
            "to compare; no regression warning can be issued this run."
        )
    elif regressed:
        names = ", ".join(f"`{a}`" for a in regressed)
        note = (
            f"> ⚠️ **Regression warning:** recall for {names} dropped by more than the "
            f"{tol_pp:g} pp tolerance. This PR is labeled `canary-regression` and "
            f"requires explicit human sign-off before merge."
        )
    else:
        note = f"> ✅ No archetype regressed by more than the {tol_pp:g} pp tolerance."
    return f"{table}\n\n{note}"


def run(
    old_path: str,
    new_path: str,
    data_path: str,
    config_path: str,
    tolerance: float | None = None,
    out_path: str | None = None,
) -> tuple[str, str]:
    """Full canary comparison. Returns ``(verdict, body)`` where verdict is
    ``"REGRESSION"`` or ``"CLEAN"``. Raises if the candidate bundle is missing
    (a genuine workflow error)."""
    with open(config_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    bench_cfg = cfg.get("model_bench", {})
    test_size = float(bench_cfg.get("test_size", 0.25))
    tol = (
        tolerance
        if tolerance is not None
        else float(bench_cfg.get("canary_tolerance", DEFAULT_TOLERANCE))
    )

    holdout = holdout_window(load_ledger(data_path), test_size)
    if "fraud_archetype" not in holdout.columns or not holdout["fraud_archetype"].notna().any():
        body = "> No fraud-archetype labels in the ledger — canary evaluation not applicable."
        if out_path:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(body + "\n")
        return "CLEAN", body

    y_te = holdout["isFraud"].astype(int).to_numpy()
    archetypes = holdout["fraud_archetype"].to_numpy()
    features = build_features(holdout)
    columns = list(features.columns)
    X_te = features.to_numpy()

    new_bundle = _load_bundle(new_path)
    if new_bundle is None:
        raise RuntimeError(f"candidate bundle missing or unreadable: {new_path}")
    _check_feature_alignment(new_bundle, columns)
    new_scores = _score_bundle_dict(new_bundle, X_te)

    old_bundle = _load_bundle(old_path)
    old_scores = None
    if old_bundle is not None:
        _check_feature_alignment(old_bundle, columns)
        old_scores = _score_bundle_dict(old_bundle, X_te)

    new_table = evaluate.per_archetype_recall(y_te, new_scores, archetypes)
    if old_scores is None:
        old_table = pd.DataFrame(columns=["archetype", "recall", "precision", "support"])
    else:
        old_table = evaluate.per_archetype_recall(y_te, old_scores, archetypes)

    rows, regressed = diff_table(old_table, new_table, tol)
    body = render_body(rows, regressed, tol, has_baseline=old_scores is not None)
    verdict = "REGRESSION" if regressed else "CLEAN"
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
    return verdict, body


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Canary/shadow evaluation: per-archetype recall diff, "
        "incumbent vs freshly retrained candidate on the same temporal holdout."
    )
    parser.add_argument("--old", required=True, help="incumbent bundle (previous commit)")
    parser.add_argument("--new", required=True, help="candidate bundle (just retrained)")
    parser.add_argument("--data", required=True, help="ledger CSV/Parquet path")
    parser.add_argument("--config", required=True, help="config.yaml path")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=None,
        help=f"recall drop that flags a regression (default {DEFAULT_TOLERANCE} = "
        "5 pp, from model_bench.canary_tolerance in config)",
    )
    parser.add_argument("--out", default=None, help="write the markdown body here")
    args = parser.parse_args()

    try:
        verdict, _ = run(args.old, args.new, args.data, args.config, args.tolerance, args.out)
    except RuntimeError as exc:
        print(f"canary: {exc}", file=sys.stderr)
        sys.exit(2)
    print(verdict)  # stdout carries ONLY the verdict — the workflow captures it


if __name__ == "__main__":
    main()
