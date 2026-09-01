"""Pattern registry."""

from __future__ import annotations

from patterns_pkg.ctx import PatternCtx


def pattern_names() -> list[str]:
    return list(_PATTERN_FNS)


def gen_pattern(name: str, ctx: PatternCtx, day: int | None = None) -> list[dict]:
    fn = _PATTERN_FNS[name]
    return fn(ctx, day)


def inject_focal_patterns(ctx: PatternCtx, *, force_easy: bool = True) -> list[dict]:
    """Sample which patterns fire for a focal persona and generate their rows.

    ``force_easy`` guarantees the deterministic easy tier (drain/dup/spike)
    whenever the window allows, so tiny-tier tests keep seeing them.
    Injection is scoped to ``ctx.day_lo..ctx.day_hi`` (one call per year for
    multi-year tiers), and medium/hard rates scale with ``ctx.scale``.
    """
    rows: list[dict] = []
    for name in EASY_PATTERNS + MEDIUM_PATTERNS + HARD_PATTERNS + HARD_NEGATIVES:
        if name in EASY_PATTERNS:
            fires = force_easy or ctx.rng.random() < PATTERN_RATES[name] * ctx.scale
        else:
            fires = ctx.rng.random() < PATTERN_RATES[name] * ctx.scale
        if fires and ctx.window_days >= _MIN_DAYS[name]:
            rows += gen_pattern(name, ctx)
    return rows


def inject_background_patterns(ctx: PatternCtx, *, bust: bool = False) -> list[dict]:
    """Fraud for a background account: optional bust-out."""
    rows: list[dict] = []
    if bust and ctx.window_days >= _MIN_DAYS["bust_out"]:
        rows += gen_bust_out(ctx)
    return rows


def apply_discovery_lag(
    rows: list[dict], rng: np.random.Generator, lag_rate: float = 0.02
) -> list[dict] -> None:
    """Label realism: ~`lag_rate` of fraud rows are only *knowable* later.

    ``label_reported_at_step`` > ``step`` mimics chargeback-reporting delay —
    the label exists in the final snapshot, but a streaming evaluator that
    only trusts labels reported up to time *t* would not see it yet.
    """
    for row in rows:
        if row["isFraud"] and rng.random() < lag_rate:
            lag_hours = int(rng.integers(24, 30 * 24))
            row["label_reported_at_step"] = int(row["step"]) + lag_hours
        else:
            row["label_reported_at_step"] = int(row["step"])
    return rows
