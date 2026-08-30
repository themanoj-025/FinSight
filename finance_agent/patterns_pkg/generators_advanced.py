"""Advanced fraud pattern generators — extract from generators.py."""
from __future__ import annotations

from finance_agent.personas import DISCRETIONARY_CATEGORIES
from finance_agent.patterns_pkg.context import PatternCtx
def gen_mimicry(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """9. Mimicry: fraud drawn from the persona's own spend distribution."""
    day = day or ctx.day_in(25, max(26, ctx.days - 5))
    category = str(ctx.rng.choice(DISCRETIONARY_CATEGORIES))
    n = int(ctx.rng.integers(1, 3))
    rows = []
    for i in range(n):
        merch = sample_merchants(ctx.rng, category, 1, force_new=ctx.used_merchants)[0]
        ctx.used_merchants.add(merch["name"])
        amount = ctx.category_amount(category)
        rows.append(
            _fraud(
                ctx,
                step=ctx.step_of(day + i, int(ctx.rng.integers(10, 21))),
                txn_type="SHOP",
                amount=amount,
                name_orig=ctx.persona.pid,
                dest="M_" + merch["name"],
                category=category,
                subcategory=merch["subcategory"],
                merchant=merch["name"],
                archetype="mimicry",
            )
        )
    return rows


def gen_account_takeover(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """10. Low-and-slow takeover: drifting baseline, then a final exfiltration.

    The migration transactions are legitimate-looking (and unlabeled); only the
    final drain transfer + cash-out are marked fraud — catching *early* is the
    model's job, which is exactly what per-archetype recall will show.
    """
    day = day or ctx.day_in(40, max(41, ctx.days - 30))
    rows: list[dict] = []
    migration_categories = ["shopping", "groceries", "transport", "entertainment"]
    for i in range(int(ctx.rng.integers(3, 6))):
        category = migration_categories[i % len(migration_categories)]
        merch = sample_merchants(ctx.rng, category, 1)[0]
        region = ctx.fresh_region() if i >= 1 else None
        rows.append(
            _row(
                ctx,
                step=ctx.step_of(day + i * 5, int(ctx.rng.integers(9, 21))),
                txn_type="SHOP",
                amount=ctx.category_amount(category),
                name_orig=ctx.persona.pid,
                dest="M_" + merch["name"],
                category=category,
                subcategory=merch["subcategory"],
                merchant=merch["name"],
                region=region,
            )
        )
    drain_day = day + 22
    victim = str(ctx.rng.choice(ctx.bg_pool)) if ctx.bg_pool else _CASH_ACCOUNT
    pair = ("account_takeover", drain_day)
    rows.append(
        _fraud(
            ctx,
            step=ctx.step_of(drain_day, 16),
            txn_type="TRANSFER",
            amount=0.0,
            name_orig=ctx.persona.pid,
            dest=victim,
            category="transfer",
            subcategory="wire",
            merchant="WireOut",
            archetype="account_takeover",
            ratio=0.75,
            pair_id=pair,
            victim=victim if victim != _CASH_ACCOUNT else None,
        )
    )
    if victim != _CASH_ACCOUNT:
        rows.append(
            _fraud(
                ctx,
                step=ctx.step_of(drain_day, 18),
                txn_type="CASH_OUT",
                amount=0.0,
                name_orig=victim,
                dest=_ATM,
                category="transfer",
                subcategory="atm",
                merchant="ATM Withdrawal",
                archetype="account_takeover",
                pair_id=pair,
            )
        )
    return rows


def gen_bust_out(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """11. Bust-out (background accounts): long trust-building, then one big drain."""
    day = day or ctx.day_in(int(ctx.days * 0.6), ctx.days - 3)
    victim = str(ctx.rng.choice(ctx.bg_pool)) if ctx.bg_pool else _CASH_ACCOUNT
    pair = ("bust_out", day)
    rows = [
        _fraud(
            ctx,
            step=ctx.step_of(day, 13),
            txn_type="TRANSFER",
            amount=0.0,
            name_orig=ctx.persona.pid,
            dest=victim,
            category="transfer",
            subcategory="wire",
            merchant="WireOut",
            archetype="bust_out",
            ratio=0.9,
            pair_id=pair,
            victim=victim if victim != _CASH_ACCOUNT else None,
        )
    ]
    if victim != _CASH_ACCOUNT:
        rows.append(
            _fraud(
                ctx,
                step=ctx.step_of(day, 15),
                txn_type="CASH_OUT",
                amount=0.0,
                name_orig=victim,
                dest=_ATM,
                category="transfer",
                subcategory="atm",
                merchant="ATM Withdrawal",
                archetype="bust_out",
                pair_id=pair,
            )
        )
    return rows


def gen_seasonal_mimicry(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """12. Seasonal-mimicry: fraud sized to blend into the holiday spend bump."""
    lo = day or max(15, int(ctx.days * 0.5))
    candidate = None
    for _ in range(60):
        d = ctx.day_in(lo, ctx.days)
        dt = ctx.date_of(d)
        if is_holiday_window(dt.month, dt.day):
            candidate = d
            break
    if candidate is None:
        return []
    rows = []
    month = ctx.date_of(candidate).month
    mult = seasonal_multiplier("shopping", month)
    for i in range(3):
        merch = sample_merchants(ctx.rng, "shopping", 1, force_new=ctx.used_merchants)[0]
        ctx.used_merchants.add(merch["name"])
        amount = round2(
            avg_amount_by_category("shopping", ctx.persona.col_multiplier)
            * mult
            * float(ctx.rng.uniform(1.0, 1.6))
        )
        rows.append(
            _fraud(
                ctx,
                step=ctx.step_of(candidate + i * 2, int(ctx.rng.integers(12, 21))),
                txn_type="SHOP",
                amount=amount,
                name_orig=ctx.persona.pid,
                dest="M_" + merch["name"],
                category="shopping",
                subcategory=merch["subcategory"],
                merchant=merch["name"],
                archetype="seasonal_mimicry",
            )
        )
    return rows


# ------------------------------------------------------------- hard negatives
def gen_life_event(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """13. Legitimate large one-time purchase / medical bill / tuition (NOT fraud)."""
    day = day or ctx.day_in(60, max(61, ctx.days - 15))
    kinds = ["large_purchase", "medical", "tuition"]
    kind = str(ctx.rng.choice(kinds))
    if kind == "large_purchase":
        merchant, category, sub, ttype = "City Motors", "shopping", "retail", "SHOP"
        amount = round2(ctx.rng.uniform(1800.0, 7000.0))
        hour = int(ctx.rng.integers(11, 19))
    elif kind == "medical":
        merchant, category, sub, ttype = "County Medical Center", "health", "clinic", "PAYMENT"
        amount = round2(ctx.rng.uniform(800.0, 4500.0))
        hour = int(ctx.rng.integers(9, 17))
    else:
        merchant, category, sub, ttype = "University Tuition", "housing", "mortgage", "PAYMENT"
        amount = round2(ctx.rng.uniform(1500.0, 6000.0))
        hour = int(ctx.rng.integers(9, 12))
    return [
        _hard_negative(
            ctx,
            step=ctx.step_of(day, hour),
            txn_type=ttype,
            amount=amount,
            name_orig=ctx.persona.pid,
            dest="M_" + merchant,
            category=category,
            subcategory=sub,
            merchant=merchant,
            archetype="hard_negative_life_event",
        )
    ]


def gen_travel(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """14. Legitimate trip to a region the persona has never used (NOT fraud)."""
    day = day or ctx.day_in(40, max(41, ctx.days - 10))
    region = ctx.fresh_region()
    duration = int(ctx.rng.integers(2, 5))
    rows = [
        _hard_negative(
            ctx,
            step=ctx.step_of(day, 16),
            txn_type="SHOP",
            amount=round2(ctx.rng.uniform(140.0, 320.0)),
            name_orig=ctx.persona.pid,
            dest="M_CityStay Hotels",
            category="housing",
            subcategory="rent",
            merchant="CityStay Hotels",
            region=region,
            archetype="hard_negative_travel",
        )
    ]
    for i in range(int(ctx.rng.integers(2, 4))):
        merch = sample_merchants(ctx.rng, "dining", 1)[0]
        rows.append(
            _hard_negative(
                ctx,
                step=ctx.step_of(day + i, int(ctx.rng.integers(12, 21))),
                txn_type="SHOP",
                amount=round2(ctx.rng.uniform(15.0, 70.0)),
                name_orig=ctx.persona.pid,
                dest="M_" + merch["name"],
                category=merch["category"],
                subcategory=merch["subcategory"],
                merchant=merch["name"],
                region=region,
                archetype="hard_negative_travel",
            )
        )
    merch = sample_merchants(ctx.rng, "transport", 1)[0]
    rows.append(
        _hard_negative(
            ctx,
            step=ctx.step_of(day + min(duration, 3), 9),
            txn_type="SHOP",
            amount=round2(ctx.rng.uniform(25.0, 80.0)),
            name_orig=ctx.persona.pid,
            dest="M_" + merch["name"],
            category=merch["category"],
            subcategory=merch["subcategory"],
            merchant=merch["name"],
            region=region,
            archetype="hard_negative_travel",
        )
    )
    return rows


def gen_rapid_burst(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """15. Legitimate rapid small-purchase burst (grocery + gas + coffee)."""
    day = day or ctx.day_in(15, max(16, ctx.days - 3))
    hour = int(ctx.rng.integers(16, 20))
    specs = [
        ("groceries", "supermarket", "SHOP", 40.0, 90.0),
        ("transport", "fuel", "SHOP", 30.0, 60.0),
        ("dining", "coffee_shops", "SHOP", 4.0, 9.0),
    ]
    rows = []
    for i, (category, sub, ttype, lo, hi) in enumerate(specs):
        merch = sample_merchants(ctx.rng, category, 1)[0]
        rows.append(
            _hard_negative(
                ctx,
                step=ctx.step_of(day, hour + i),
                txn_type=ttype,
                amount=round2(ctx.rng.uniform(lo, hi)),
                name_orig=ctx.persona.pid,
                dest="M_" + merch["name"],
                category=category,
                subcategory=sub,
                merchant=merch["name"],
                archetype="hard_negative_rapid_burst",
            )
        )
    return rows


# ----------------------------------------------------------------- orchestrator
_PATTERN_FNS: dict[str, Callable[..., list[dict]]] = {
    "balance_drain": gen_balance_drain,
    "duplicate_charge": gen_duplicate_charge,
    "spend_spike": gen_spend_spike,
    "card_testing": gen_card_testing,
    "slow_balance_drain": gen_slow_balance_drain,
    "new_payee_transfer": gen_new_payee_transfer,
    "subscription_creep": gen_subscription_creep,
    "refund_abuse": gen_refund_abuse,
    "mimicry": gen_mimicry,
    "account_takeover": gen_account_takeover,
    "bust_out": gen_bust_out,
    "seasonal_mimicry": gen_seasonal_mimicry,
    "life_event": gen_life_event,
    "travel": gen_travel,
    "rapid_burst": gen_rapid_burst,
}


