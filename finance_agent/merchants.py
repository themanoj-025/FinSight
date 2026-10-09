"""Synthetic merchant taxonomy, regions, and seasonality (Data-Gen §4).

This module owns every piece of "place" and "merchant" realism in the
generator so the rest of the pipeline can stay declarative:

  * a deterministic catalog of ~300 **named synthetic merchants**, generated
    combinatorially from curated name pools and weighted Zipfian-popularity so
    a handful of dominant chains carry most of the volume (never uniform);
  * a lightweight **category hierarchy**: flat ``category`` (unchanged from
    before, so existing consumers keep working) -> ``subcategory`` (leaf,
    e.g. ``coffee_shops`` under ``dining``), plus ``category_group``
    (income / fixed / discretionary / savings / transfer);
  * a set of ~36 synthetic regions with coordinates so distance-from-home
    becomes a computable fraud signal;
  * month-of-year spending multipliers per category (seasonality §3).

Everything here is deterministic and import-only — no RNG lives in this
module (callers pass their own ``numpy.random.Generator``).
"""

from __future__ import annotations

import itertools
from math import asin, cos, radians, sin, sqrt

import numpy as np

# ------------------------------------------------------------------ regions
# (city, state, lat, lon) — synthetic but plausible US-style geography.
_REGION_TABLE: list[tuple[str, str, float, float]] = [
    ("Portland", "OR", 45.52, -122.68),
    ("Austin", "TX", 30.27, -97.74),
    ("Denver", "CO", 39.74, -104.99),
    ("Seattle", "WA", 47.61, -122.33),
    ("Minneapolis", "MN", 44.98, -93.27),
    ("Nashville", "TN", 36.16, -86.78),
    ("Charlotte", "NC", 35.23, -80.84),
    ("Columbus", "OH", 39.96, -83.00),
    ("Indianapolis", "IN", 39.77, -86.16),
    ("San Antonio", "TX", 29.42, -98.49),
    ("Sacramento", "CA", 38.58, -121.49),
    ("Raleigh", "NC", 35.78, -78.64),
    ("Milwaukee", "WI", 43.04, -87.91),
    ("Kansas City", "MO", 39.10, -94.58),
    ("Salt Lake City", "UT", 40.76, -111.89),
    ("Tucson", "AZ", 32.22, -110.97),
    ("New Orleans", "LA", 29.95, -90.07),
    ("Louisville", "KY", 38.25, -85.76),
    ("Oklahoma City", "OK", 35.47, -97.52),
    ("Buffalo", "NY", 42.89, -78.88),
    ("Omaha", "NE", 41.26, -95.93),
    ("Albuquerque", "NM", 35.08, -106.65),
    ("El Paso", "TX", 31.76, -106.49),
    ("Fresno", "CA", 36.74, -119.77),
    ("Grand Rapids", "MI", 42.96, -85.66),
    ("Knoxville", "TN", 35.96, -83.92),
    ("Spokane", "WA", 47.66, -117.43),
    ("Boise", "ID", 43.62, -116.20),
    ("Madison", "WI", 43.07, -89.40),
    ("Richmond", "VA", 37.54, -77.44),
    ("Providence", "RI", 41.82, -71.41),
    ("Hartford", "CT", 41.76, -72.67),
    ("Wilmington", "DE", 39.74, -75.55),
    ("Manchester", "NH", 42.99, -71.45),
    ("Burlington", "VT", 44.48, -73.21),
    ("Portland", "ME", 43.66, -70.25),
]

REGIONS: dict[str, dict[str, float | str]] = {
    f"R{i:02d}_{city.lower().replace(' ', '')}": {
        "city": city,
        "state": state,
        "lat": lat,
        "lon": lon,
        "index": float(i),
    }
    for i, (city, state, lat, lon) in enumerate(_REGION_TABLE)
}

REGION_IDS: list[str] = list(REGIONS)


def haversine_miles(region_a: str, region_b: str) -> float:
    """Great-circle distance between two regions in miles (0.0 if same)."""
    a, b = REGIONS[region_a], REGIONS[region_b]
    lat1, lon1, lat2, lon2 = map(
        radians, (float(a["lat"]), float(a["lon"]), float(b["lat"]), float(b["lon"]))
    )
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return float(2 * 3958.8 * asin(sqrt(h)))


# ------------------------------------------------------------ category tree
# `category` stays the flat value the rest of the project already understands;
# `subcategory` is the leaf for drill-down; `category_group` is the coarse
# parent bucket (income / fixed / discretionary / savings / transfer).
CATEGORY_GROUP: dict[str, str] = {
    "income": "income",
    "refund": "income",
    "housing": "fixed",
    "utilities": "fixed",
    "subscriptions": "fixed",
    "credit": "fixed",
    "groceries": "discretionary",
    "dining": "discretionary",
    "transport": "discretionary",
    "entertainment": "discretionary",
    "shopping": "discretionary",
    "health": "discretionary",
    "savings": "savings",
    "transfer": "transfer",
}

SUBCATEGORIES: dict[str, list[str]] = {
    "dining": ["coffee_shops", "restaurants", "fast_food"],
    "groceries": ["supermarket", "warehouse", "convenience"],
    "shopping": ["online", "retail", "department_store", "home"],
    "transport": ["rideshare", "fuel", "transit", "parking"],
    "utilities": ["electricity", "water", "internet", "phone"],
    "entertainment": ["streaming", "cinema", "gaming", "events"],
    "health": ["pharmacy", "clinic", "gym"],
    "subscriptions": ["streaming", "software", "fitness"],
    "housing": ["rent", "mortgage"],
    "income": ["payroll", "freelance", "pension", "deposit"],
    "savings": ["auto_transfer"],
    "transfer": ["p2p", "wire", "atm", "internal"],
    "refund": ["merchant_refund"],
    "credit": ["credit_payment"],
}

# Transaction type for a (category, subcategory) pair.
_TYPE_OVERRIDES: dict[tuple[str, str], str] = {
    ("income", "payroll"): "SALARY",
    ("income", "pension"): "SALARY",
    ("income", "freelance"): "CASH_IN",
    ("income", "deposit"): "CASH_IN",
    ("refund", "merchant_refund"): "CASH_IN",
    ("housing", "rent"): "TRANSFER",
    ("housing", "mortgage"): "TRANSFER",
    ("savings", "auto_transfer"): "TRANSFER",
    ("transfer", "p2p"): "TRANSFER",
    ("transfer", "wire"): "TRANSFER",
    ("transfer", "internal"): "TRANSFER",
    ("transfer", "atm"): "CASH_OUT",
    ("credit", "credit_payment"): "TRANSFER",
}
for _sub in ("streaming", "software", "fitness"):
    _TYPE_OVERRIDES[("subscriptions", _sub)] = "SUBSCRIPTION"
DEFAULT_TYPE = {"utilities": "PAYMENT", "housing": "TRANSFER", "savings": "TRANSFER"}
SHOP_TYPES = {"groceries", "dining", "transport", "entertainment", "shopping", "health"}


def type_for(category: str, subcategory: str) -> str:
    if (category, subcategory) in _TYPE_OVERRIDES:
        return _TYPE_OVERRIDES[(category, subcategory)]
    if category in DEFAULT_TYPE:
        return DEFAULT_TYPE[category]
    return "SHOP" if category in SHOP_TYPES else "TRANSFER"


# ------------------------------------------------------- seasonality (§3)
# Month-of-year spend multiplier per category (Jan=0 ... Dec=11). Flat
# categories (income, savings, transfer, refund, credit) are implicitly 1.0.
SEASONAL_MULTIPLIER: dict[str, list[float]] = {
    "groceries": [1.05, 1.0, 1.0, 1.0, 1.0, 1.0, 1.05, 1.1, 1.05, 1.1, 1.2, 1.3],
    "dining": [0.65, 0.9, 1.0, 1.0, 1.05, 1.0, 1.05, 1.0, 1.05, 1.1, 1.2, 1.35],
    "transport": [0.9, 0.95, 1.0, 1.0, 1.05, 1.1, 1.2, 1.15, 1.05, 1.0, 1.1, 1.2],
    "utilities": [1.35, 1.25, 1.05, 0.9, 0.85, 0.9, 1.05, 1.25, 1.15, 0.95, 1.05, 1.2],
    "entertainment": [0.9, 0.95, 1.0, 1.0, 1.05, 1.1, 1.2, 1.05, 1.0, 1.0, 1.1, 1.3],
    "shopping": [0.85, 0.9, 0.95, 1.0, 1.05, 1.0, 1.05, 1.2, 1.05, 1.1, 1.5, 1.7],
    "health": [1.1, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.05, 1.05, 1.1],
    "subscriptions": [1.0] * 12,
    "housing": [1.0] * 12,
}


def seasonal_multiplier(category: str, month_of_year: int) -> float:
    """Spend multiplier for `category` in month 1..12 (1.0 when flat)."""
    return SEASONAL_MULTIPLIER.get(category, [1.0] * 12)[month_of_year - 1]


def is_holiday_window(month: int, day: int) -> bool:
    """Nov 15 - Dec 31 — the elevated-spend window (§3 / pattern 12)."""
    return (month == 11 and day >= 15) or month == 12


# ------------------------------------------------------------ subscriptions
# Shared with the legacy generator (generate_data re-exports this object, so
# `monkeypatch.setitem(generate_data.SUBSCRIPTION_AMOUNTS, ...)` keeps working).
SUBSCRIPTION_AMOUNTS: dict[str, float] = {
    "Netflix": 15.49,
    "Spotify": 9.99,
    "Planet Fitness": 34.99,
    "Verizon": 40.0,
    "iCloud": 2.99,
    "Max (streaming)": 15.99,
    "YouTube Premium": 13.99,
    "Adobe CC": 22.99,
}


# ------------------------------------------------------------ merchant pool
# Curated prefix/suffix pools per subcategory — the product, taken in a fixed
# order, produces deterministic *named* merchants. A handful of curated
# single names (including the legacy ones) anchor each category.
_MERCHANT_POOLS: dict[str, list[str]] = {
    "supermarket": ["Fresh", "Green", "Harvest", "Golden", "Family", "Sunny", "Meadow", "Coastal"],
    "groceries_suffix": ["Market", "Grocers", "Foods", "Pantry", "Provisions", "Mart", "Basket"],
    "restaurants": [
        "The Copper",
        "Olive",
        "Ember",
        "Sage",
        "Brick",
        "Golden",
        "Harbor",
        "Willow",
    ],
    "restaurants_suffix": ["Kitchen", "Grill", "Table", "House", "Bistro", "Tavern", "Cantina"],
    "fast_food": ["Burger", "Taco", "Pizza", "Wing", "Chicken", "Sub", "Falafel", "Ramen"],
    "fast_food_suffix": ["Joint", "Express", "Shack", "Stand", "Corner", "Stop"],
    "coffee_shops": ["Cafe", "Bean", "Roast", "Brew", "Cup", "Grounds", "Mug", "Java"],
    "coffee_shops_suffix": ["House", "Bar", "Corner", "Company", "Supply", "Room"],
    "online": ["Apex", "Nova", "Orbit", "Lumen", "Vertex", "Pulse", "Skyline", "Northwind"],
    "online_suffix": ["Market", "Store", "Goods", "Emporium", "Outlet", "Warehouse"],
    "retail": [
        "Main Street",
        "Union",
        "Regency",
        "Pioneer",
        "Corner",
        "Fairview",
        "Crown",
        "Maple",
    ],
    "retail_suffix": ["Clothiers", "General Store", "Goods", "Outlet", "Market"],
    "department_store": ["Grand", "Central", "Metro", "Empire", "Regal", "Cityline"],
    "department_store_suffix": ["Department Store", "Galleria", "Plaza", "Mall"],
    "home": ["Hearth", "Nest", "Casa", "Harbor", "Timber", "Stone", "Willow"],
    "home_suffix": ["Home Goods", "Furnishings", "Supply", "Living"],
    "rideshare": ["RideOn", "CityHop", "SwiftGo", "LoopRide", "StreetCar"],
    "fuel": ["Petro", "FillUp", "Highway", "Crossroads", "Roadside"],
    "fuel_suffix": ["Gas", "Fuel", "Station"],
    "transit": ["MetroCard", "City Transit", "Subway", "BusLink", "Regional Rail"],
    "parking": ["ParkCentral", "Downtown Parking", "AirportPark", "Garage 24", "LotPro"],
    "electricity": [
        "PowerGrid",
        "CityPower",
        "Summit Electric",
        "Evergreen Utilities",
        "State Grid",
    ],
    "water": ["City Water", "AquaSource", "Metro Water", "Harbor Utilities"],
    "internet": ["FiberNet", "BroadbandX", "ConnectHub", "WaveLink"],
    "phone": ["CellWave", "MobileOne", "SkyTel", "TowerTalk"],
    "streaming": ["StarPlay", "MediaStream", "FlickStream", "CinePlay"],
    "cinema": ["Grand Cinemas", "Regal Screen", "Metro Cinema", "Starlight Theaters", "CineWorld"],
    "gaming": ["PixelForge", "GameHive", "ArcadeOne", "QuestWorks", "PlayStorm"],
    "events": ["TicketHub", "EventCenter", "ArenaBox", "ShowTime"],
    "pharmacy": ["CarePlus", "MedHealth", "PharmEasy", "WellCare Pharmacy", "CityMed"],
    "clinic": ["Northside Clinic", "FamilyMed", "HealthFirst", "Summit Care", "Lakeview Medical"],
    "gym": ["FitZone", "IronWorks", "FlexPoint", "CoreStrength", "PulseFit"],
    "software": ["SoftKey", "CloudDesk", "AppSuite", "DevTools+", "PixelStudio"],
    "rent": [
        "Oakwood Property Mgmt",
        "Maple Ridge Realty",
        "CityRent",
        "Harborview Properties",
        "Summit Property Group",
    ],
    "mortgage": [
        "FirstHome Mortgage",
        "Union Mortgage",
        "Metro Loan Servicing",
        "Student Loan Servicer",
    ],
    "payroll": [
        "Acme Corp Payroll",
        "Northstar Industries Payroll",
        "TechBridge Solutions Payroll",
        "BlueRock Consulting",
        "CityWorks Payroll",
        "State University Payroll",
    ],
    "freelance": ["Freelance Income", "GigPay", "ContractWorks", "UpWorkflow"],
    "pension": ["Federal Benefits", "State Pension Fund", "Railroad Pension Board"],
    "deposit": ["Deposit", "External Transfer", "Wire Deposit"],
    "p2p": ["Peer Transfer", "QuickPay", "InstantSend", "PayFriend"],
    "wire": ["WireOut", "BankWire", "InterBank Transfer"],
    "atm": ["ATM Withdrawal", "ATM Network"],
    "internal": ["Internal Transfer", "Account Transfer"],
    "credit_payment": ["Credit Card Payment", "Card Autopay", "Bank Card Services"],
    "auto_transfer": ["AutoSavings", "SmartSaver"],
}


def _build_merchants() -> list[dict[str, str]]:
    """Deterministic catalog of named merchants (category/subcategory/type).

    ``seen`` is hoisted outside the loops so merchant names are unique across
    the *whole* catalog — ``merchant_by_name`` and the `M_<name>` destination
    ids both rely on it.
    """
    merchants: list[dict[str, str]] = []
    seen: set[str] = set()
    for category, subs in SUBCATEGORIES.items():
        for sub in subs:
            prefix_pool = _MERCHANT_POOLS.get(sub)
            suffix_pool = _MERCHANT_POOLS.get(f"{sub}_suffix")
            names: list[str] = list(_MERCHANT_POOLS.get(sub, []))  # curated singles first
            if prefix_pool and suffix_pool:
                for prefix, suffix in itertools.product(prefix_pool, suffix_pool):
                    names.append(f"{prefix} {suffix}")
            # dedupe against the whole catalog, cap per subcategory
            taken = 0
            for name in names:
                if name in seen or name in {"", " "}:
                    continue
                seen.add(name)
                merchants.append(
                    {
                        "name": name,
                        "category": category,
                        "subcategory": sub,
                        "type": type_for(category, sub),
                    }
                )
                taken += 1
                if taken >= 12:
                    break
    return merchants


MERCHANTS: list[dict[str, str]] = _build_merchants()

# Zipfian popularity per merchant (rank by catalog order, 1/(rank+k)^s): a few
# dominant chains, a long tail — the shape test in test_merchants.py asserts
# the top-10 share stays in a believable band.
_ZIPF_S = 0.9
MERCHANT_WEIGHTS: np.ndarray = np.asarray(
    [1.0 / (rank + 2.0) ** _ZIPF_S for rank in range(len(MERCHANTS))], dtype=float
)
MERCHANT_WEIGHTS /= MERCHANT_WEIGHTS.sum()

MERCHANTS_BY_CATEGORY: dict[str, list[int]] = {c: [] for c in SUBCATEGORIES}
for _i, _m in enumerate(MERCHANTS):
    MERCHANTS_BY_CATEGORY.setdefault(_m["category"], []).append(_i)

_CATEGORY_WEIGHTS: dict[str, np.ndarray] = {
    cat: MERCHANT_WEIGHTS[idx] / MERCHANT_WEIGHTS[idx].sum()
    for cat, idx in MERCHANTS_BY_CATEGORY.items()
}


def sample_merchants(
    rng: np.random.Generator, category: str, n: int, *, force_new: set[str] | None = None
) -> list[dict[str, str]]:
    """Sample `n` merchant records for `category` (Zipfian-weighted).

    ``force_new`` excludes previously-used merchant names, so fraud patterns
    can build "never-seen merchant" events deterministically.
    """
    if not n:
        return []
    idx = MERCHANTS_BY_CATEGORY[category]
    weights = _CATEGORY_WEIGHTS[category]
    chosen = rng.choice(np.asarray(idx, dtype=int), size=int(n), replace=True, p=weights)
    out = [MERCHANTS[int(i)] for i in chosen]
    if force_new:
        replacement_pool = [
            MERCHANTS[int(i)] for i in idx if MERCHANTS[int(i)]["name"] not in force_new
        ]
        if replacement_pool and len(replacement_pool) >= n:
            out = [
                m if m["name"] not in force_new else replacement_pool[i % len(replacement_pool)]
                for i, m in enumerate(out)
            ]
    return out


def merchant_by_name(name: str) -> dict[str, str]:
    """Catalog record for `name` (merchant names are unique)."""
    for m in MERCHANTS:
        if m["name"] == name:
            return m
    raise KeyError(f"unknown merchant {name!r}")
