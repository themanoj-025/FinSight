"""Fraud pattern generators."""

from __future__ import annotations

from patterns_pkg.ctx import PatternCtx


def gen_balance_drain(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """1. Balance-draining transfer + rapid cash-out (isFraud=1).

    Amount is sized to ~60% of the live balance by the balance pass
    (``_drain_ratio``), leaving ~40% — the classic easy signature.
    """
    day = day or ctx.day_in(4, max(5, min(10, ctx.days - 4)))
    victim = str(ctx.rng.choice(ctx.bg_pool)) if ctx.bg_pool else _CASH_ACCOUNT
    pair = ("balance_drain", day)
    rows = [
        _fraud(
            ctx,
            step=ctx.step_of(day, 14),
            txn_type="TRANSFER",
            amount=0.0,
            name_orig=ctx.persona.pid,
            dest=victim,
            category="transfer",
            subcategory="wire",
            merchant="WireOut",
            archetype="balance_drain",
            ratio=0.6,
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
                archetype="balance_drain",
                pair_id=pair,
            )
        )
    return rows


def gen_duplicate_charge(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """2. Duplicate charge: same merchant + amount twice (anomaly, not fraud)."""
    day = day or ctx.day_in(15, max(16, min(30, ctx.days - 10)))
    merch = sample_merchants(ctx.rng, "entertainment", 1, force_new=ctx.used_merchants)[0]
    ctx.used_merchants.add(merch["name"])
    amount = round2(ctx.rng.uniform(39.99, 89.99))
    rows = [
        _row(
            ctx,
            step=ctx.step_of(day, 19),
            txn_type="SHOP",
            amount=amount,
            name_orig=ctx.persona.pid,
            dest="M_" + merch["name"],
            category=merch["category"],
            subcategory=merch["subcategory"],
            merchant=merch["name"],
        ),
        _row(
            ctx,
            step=ctx.step_of(day, 21),
            txn_type="SHOP",
            amount=amount,
            name_orig=ctx.persona.pid,
            dest="M_" + merch["name"],
            category=merch["category"],
            subcategory=merch["subcategory"],
            merchant=merch["name"],
            is_anomaly=1,
            anomaly_type="duplicate_charge",
            archetype="duplicate_charge",
        ),
    ]
    return rows


def gen_spend_spike(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """3. Category spending spike (anomaly, not fraud) — ~4x daily baseline."""
    day = day or ctx.day_in(20, max(21, min(60, ctx.days - 5)))
    category = "dining"
    rows = []
    for hour in (12, 13, 18, 19):
        amount = round2(ctx.rng.uniform(45.0, 90.0))
        merch = sample_merchants(ctx.rng, category, 1)[0]
        rows.append(
            _row(
                ctx,
                step=ctx.step_of(day, hour),
                txn_type="SHOP",
                amount=amount,
                name_orig=ctx.persona.pid,
                dest="M_" + merch["name"],
                category=category,
                subcategory=merch["subcategory"],
                merchant=merch["name"],
                is_anomaly=1,
                anomaly_type="spend_spike",
                archetype="spend_spike",
            )
        )
    return rows


# --------------------------------------------------------------- medium tier
def gen_card_testing(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """4. Card testing: burst of small charges then one large charge."""
    day = day or ctx.day_in(10, max(11, min(40, ctx.days - 5)))
    rows = []
    hour = int(ctx.rng.integers(9, 20))
    n_small = int(ctx.rng.integers(3, 6))
    for _ in range(n_small):
        merch = sample_merchants(ctx.rng, "transport", 1, force_new=ctx.used_merchants)[0]
        ctx.used_merchants.add(merch["name"])
        rows.append(
            _fraud(
                ctx,
                step=ctx.step_of(day, hour),
                txn_type="SHOP",
                amount=round2(ctx.rng.uniform(2.0, 7.5)),
                name_orig=ctx.persona.pid,
                dest="M_" + merch["name"],
                category=merch["category"],
                subcategory=merch["subcategory"],
                merchant=merch["name"],
                archetype="card_testing",
            )
        )
    merch = sample_merchants(ctx.rng, "shopping", 1, force_new=ctx.used_merchants)[0]
    rows.append(
        _fraud(
            ctx,
            step=ctx.step_of(day, hour + int(ctx.rng.integers(1, 3))),
            txn_type="SHOP",
            amount=round2(ctx.rng.uniform(140.0, 320.0)),
            name_orig=ctx.persona.pid,
            dest="M_" + merch["name"],
            category=merch["category"],
            subcategory=merch["subcategory"],
            merchant=merch["name"],
            archetype="card_testing",
        )
    )
    return rows


def gen_slow_balance_drain(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """5. Slow balance drain: many small transfers over 2-3 weeks (rolling-window fraud)."""
    day = day or ctx.day_in(20, max(21, ctx.days - 25))
    payee = "C_NewPayee_" + str(int(ctx.rng.integers(1000, 9999)))
    ctx.used_dests.add(payee)
    n = int(ctx.rng.integers(5, 9))
    rows = []
    for i in range(n):
        rows.append(
            _fraud(
                ctx,
                step=ctx.step_of(day + i * 3 + int(ctx.rng.integers(0, 2)), 13),
                txn_type="TRANSFER",
                amount=round2(ctx.rng.uniform(40.0, 160.0)),
                name_orig=ctx.persona.pid,
                dest=payee,
                category="transfer",
                subcategory="p2p",
                merchant="Peer Transfer",
                archetype="slow_balance_drain",
            )
        )
    return rows


def gen_new_payee_transfer(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """6. First-time-region access + large transfer to a never-seen payee."""
    day = day or ctx.day_in(15, max(16, min(45, ctx.days - 5)))
    fresh = ctx.fresh_region()
    merch = sample_merchants(ctx.rng, "shopping", 1)[0]
    payee = "C_NewPayee_" + str(int(ctx.rng.integers(1000, 9999)))
    ctx.used_dests.add(payee)
    rows = [
        _fraud(
            ctx,
            step=ctx.step_of(day, 10),
            txn_type="SHOP",
            amount=round2(ctx.rng.uniform(12.0, 40.0)),
            name_orig=ctx.persona.pid,
            dest="M_" + merch["name"],
            category=merch["category"],
            subcategory=merch["subcategory"],
            merchant=merch["name"],
            region=fresh,
            archetype="new_payee_transfer",
        ),
        _fraud(
            ctx,
            step=ctx.step_of(day, 14),
            txn_type="TRANSFER",
            amount=round2(ctx.rng.uniform(900.0, 3500.0)),
            name_orig=ctx.persona.pid,
            dest=payee,
            category="transfer",
            subcategory="wire",
            merchant="WireOut",
            archetype="new_payee_transfer",
        ),
    ]
    return rows


def gen_subscription_creep(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """7. Subscription creep: several new small recurring charges in a week."""
    day = day or ctx.day_in(20, max(21, ctx.days - 10))
    n = int(ctx.rng.integers(3, 6))
    rows = []
    for i in range(n):
        merch = sample_merchants(ctx.rng, "subscriptions", 1, force_new=ctx.used_merchants)[0]
        ctx.used_merchants.add(merch["name"])
        rows.append(
            _fraud(
                ctx,
                step=ctx.step_of(day + i * 2, 10),
                txn_type="SUBSCRIPTION",
                amount=round2(ctx.rng.uniform(4.99, 29.99)),
                name_orig=ctx.persona.pid,
                dest="M_" + merch["name"],
                category="subscriptions",
                subcategory=merch["subcategory"],
                merchant=merch["name"],
                archetype="subscription_creep",
            )
        )
    return rows


def gen_refund_abuse(ctx: PatternCtx, day: int | None = None) -> list[dict]:
    """8. Refund / chargeback abuse loop: purchase -> refund -> repurchase, repeated."""
    day = day or ctx.day_in(30, max(31, ctx.days - 45))
    loops = int(ctx.rng.integers(2, 4))
    rows: list[dict] = []
    merchant_name = "LuxeOnline"
    merch = next(m for m in merchants.MERCHANTS if m["category"] == "shopping")
    merchant_name = merch["name"]
    for k in range(loops):
        base = day + k * 12
        amt = round2(ctx.rng.uniform(60.0, 180.0))
        rows.append(
            _fraud(
                ctx,
                step=ctx.step_of(base, 18),
                txn_type="SHOP",
                amount=amt,
                name_orig=ctx.persona.pid,
                dest="M_" + merchant_name,
                category="shopping",
                subcategory="online",
                merchant=merchant_name,
                archetype="refund_abuse",
            )
        )
        rows.append(
            _fraud(
                ctx,
                step=ctx.step_of(base + int(ctx.rng.integers(1, 3)), 11),
                txn_type="CASH_IN",
                amount=amt,
                name_orig=ctx.persona.pid,
                dest="M_" + merchant_name,
                category="refund",
                subcategory="merchant_refund",
                merchant=merchant_name,
                archetype="refund_abuse",
            )
        )
        rows.append(
            _fraud(
                ctx,
                step=ctx.step_of(base + int(ctx.rng.integers(3, 6)), 19),
                txn_type="SHOP",
                amount=amt,
                name_orig=ctx.persona.pid,
                dest="M_" + merchant_name,
                category="shopping",
                subcategory="online",
                merchant=merchant_name,
                archetype="refund_abuse",
            )
        )
    return rows


# ------------------------------------------------------------------ hard tier
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


