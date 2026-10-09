"""Generate a deterministic, PaySim-style synthetic personal-finance dataset.

This is the **CLI + compatibility layer**; all generation logic lives in
``finance_agent/datagen.py`` (vectorized persona ledgers, multi-account
structure, seasonality/drift/life events, background population, clamped
balance pass, derived columns). See ``docs/DataGeneration.md`` for the full
design.

Tier system (``--tier``, defaults from ``config.yaml data.tier``):

  * ``tiny``  — legacy footprint (~20 background accounts). Fast tests + CI.
  * ``demo``  — the app / README tier (~2,000 background accounts). This is the
    default for ``make data`` and the Streamlit app.
  * ``bench`` — the model-benchmark tier (~20,000 background accounts, medium/
    hard fraud rates scaled down to keep class imbalance defensible). Writes
    Parquet (``data/transactions.parquet``) by default.

Output format is chosen by the ``--output`` extension (``.parquet`` /
``.csv``) or ``--format``; bench defaults to Parquet, tiny/demo to CSV. The
legacy ``generate()`` function is preserved with its original signature so
existing consumers and tests keep working — it generates a tiny-tier ledger
with the same columns plus the new additive ones.

Usage:
    python generate_data.py                       # demo tier -> data/transactions.csv
    python generate_data.py --tier tiny
    python generate_data.py --tier bench          # -> data/transactions.parquet
    python generate_data.py --days 90 --seed 42
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pandas as pd

from finance_agent import datagen
from finance_agent.merchants import SUBSCRIPTION_AMOUNTS

# ---------------------------------------------------------------------------
# Backward-compatible re-export: SUBSCRIPTION_AMOUNTS is the canonical object
# from the merchant catalog module, so
# `monkeypatch.setitem(generate_data.SUBSCRIPTION_AMOUNTS, ...)` keeps working
# against the same dict the generator reads.
# ---------------------------------------------------------------------------


def subscription_total() -> float:
    """Total monthly subscription spend, derived from the source list (no literals)."""
    return round(sum(SUBSCRIPTION_AMOUNTS.values()), 2)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _load_config(config_path: str) -> dict[str, Any]:
    """Load the `data` section of config.yaml ({} when missing/invalid)."""
    cfg: dict[str, Any] = {}
    if config_path and os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as fh:
            try:
                import yaml

                cfg = yaml.safe_load(fh) or {}
            except (yaml.YAMLError, OSError, ValueError):
                cfg = {}
    return cfg.get("data", {}) or {}


def generate(
    days: int = 90,
    seed: int = 42,
    user: str = "U_Alex",
    focal_users: list[str] | None = None,
    n_background_accounts: int | None = None,
    start_date: str = "2025-01-01",
    n_fraud_pairs: int = 20,
) -> pd.DataFrame:
    """Generate the full synthetic dataset deterministically (legacy signature).

    Wraps ``datagen.generate_dataset`` at the ``tiny`` tier so existing callers
    (tests, docs examples) keep the same footprint while gaining all the new
    additive columns. ``user`` is the legacy single-focal-user name;
    ``focal_users`` (when given) enables multi-user mode. ``n_fraud_pairs`` is
    accepted for signature compatibility (the new generator derives fraud from
    the pattern library instead of the old pair-injection loop).
    """
    users = list(focal_users) if focal_users else [user]
    return datagen.generate_dataset(
        days=days,
        seed=seed,
        tier="tiny",
        users=users,
        n_background_accounts=n_background_accounts,
        start_date=start_date,
        n_fraud_pairs=n_fraud_pairs,
    )


def _resolve_args(
    args: argparse.Namespace, defaults: dict[str, Any]
) -> tuple[dict[str, Any], str, str]:
    """Merge CLI args over config defaults; returns (kwargs, tier, format)."""
    tier = str(args.tier or defaults.get("tier", "demo"))
    if tier not in datagen.TIER_DEFAULTS:
        raise ValueError(f"unknown tier {tier!r} (expected tiny|demo|bench)")

    # Explicit None checks: `--seed 0` and `--n-background-accounts 0` are
    # valid values and must never be overridden by defaults (0 is falsy).
    td = datagen.TIER_DEFAULTS[tier]
    kwargs: dict[str, Any] = {}
    if args.days is not None:
        kwargs["days"] = int(args.days)
    elif tier == "bench":
        # The bench tier owns its multi-year window (Data-Gen §3): the generic
        # config `days` (an app/tiny/demo knob) must never shrink it to 90.
        kwargs["days"] = int(td.get("days", 1460))
    else:
        kwargs["days"] = int(defaults.get("days", td.get("days", 90)))
    if args.seed is not None:
        kwargs["seed"] = int(args.seed)
    else:
        kwargs["seed"] = int(defaults.get("seed", 42))
    user = args.user if args.user is not None else str(defaults.get("focal_user", "U_Alex"))
    if args.focal_users:
        kwargs["users"] = [u.strip() for u in args.focal_users.split(",") if u.strip()]
    elif tier == "bench":
        # The bench tier defaults to a 200-persona focal population so the
        # fraud rate lands in the defensible 0.1-0.5% band (Data-Gen §1/§5);
        # `--focal-users` still overrides.
        kwargs["users"] = datagen.focal_user_ids(int(td.get("focal_users", 200)))
    else:
        configured = defaults.get("focal_users")
        if isinstance(configured, list) and configured:
            kwargs["users"] = [str(u) for u in configured]
        else:
            kwargs["users"] = [user]
    if args.n_background_accounts is not None:
        kwargs["n_background_accounts"] = int(args.n_background_accounts)
    elif "n_background_accounts" in defaults and tier == "tiny":
        kwargs["n_background_accounts"] = int(defaults["n_background_accounts"])
    kwargs["start_date"] = (
        args.start_date
        if args.start_date is not None
        else str(defaults.get("start_date", "2025-01-01"))
    )
    if args.n_fraud_pairs is not None:
        kwargs["n_fraud_pairs"] = int(args.n_fraud_pairs)

    fmt = str(args.format or "")
    if args.output:
        out = args.output
    elif fmt == "parquet" or tier == "bench":
        out = str(defaults.get("bench_path", "data/transactions.parquet"))
    else:
        out = str(defaults.get("path", "data/transactions.csv"))
    if not fmt:
        fmt = "parquet" if out.lower().endswith((".parquet", ".pq")) else "csv"
    return kwargs, tier, fmt


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic personal-finance data.")
    parser.add_argument("--days", type=int, help="Number of days to simulate.")
    parser.add_argument("--seed", type=int, help="Random seed for reproducibility.")
    parser.add_argument("--user", type=str, help="Focal user account name.")
    parser.add_argument(
        "--focal-users",
        type=str,
        help="Comma-separated focal user names (multi-user mode; overrides --user).",
    )
    parser.add_argument("--n-background-accounts", type=int, help="Background account pool size.")
    parser.add_argument("--start-date", type=str, help="Simulation start date (YYYY-MM-DD).")
    parser.add_argument("--n-fraud-pairs", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--tier", choices=sorted(datagen.TIER_DEFAULTS), help="Generation tier.")
    parser.add_argument("--format", choices=["csv", "parquet"], help="Output format override.")
    parser.add_argument("--output", type=str, default=None, help="Output path (CSV or Parquet).")
    parser.add_argument("--config", type=str, default="config.yaml", help="Path to config.yaml.")
    parser.add_argument("--verbose", action="store_true", help="Print progress to stderr.")
    args = parser.parse_args()

    defaults = _load_config(args.config)
    kwargs, tier, fmt = _resolve_args(args, defaults)

    df = datagen.generate_dataset(**kwargs, tier=tier, verbose=args.verbose)
    stats = datagen.tier_stats(df, tier)

    out = args.output or (
        str(defaults.get("bench_path", "data/transactions.parquet"))
        if fmt == "parquet"
        else str(defaults.get("path", "data/transactions.csv"))
    )
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if fmt == "parquet":
        df.to_parquet(out, index=False)
    else:
        df.to_csv(out, index=False)

    print(f"Wrote {out} ({tier} tier)")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
