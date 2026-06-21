"""
seed_demo.py — Populate the database with realistic demo data for the Nitra region.

Run:  python seed_demo.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import random
import logging
from models import init_db, SessionLocal
from utils.engine import RawListing, upsert_listing

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

random.seed(42)

# ── Market baselines (€/m²) per city + type — approximate real Slovak market values ──
MARKET_BASELINES = {
    ("Nitra", "byt"):       2_400,
    ("Nitra", "dom"):       1_900,
    ("Nitra", "pozemok"):     130,
    ("Šurany", "byt"):      1_450,
    ("Šurany", "dom"):      1_200,
    ("Zlaté Moravce", "byt"): 1_600,
    ("Zlaté Moravce", "dom"): 1_300,
    ("Vráble", "byt"):      1_350,
    ("Vráble", "dom"):      1_100,
    ("Mojmírovce", "dom"):    950,
}

CITIES = [
    "Nitra", "Nitra", "Nitra",   # higher weight for Nitra
    "Šurany", "Zlaté Moravce", "Vráble", "Mojmírovce",
]

PTYPES = ["byt", "byt", "dom", "dom", "pozemok"]

SUBTYPES_BYT = ["1-izbový byt", "2-izbový byt", "3-izbový byt", "4-izbový byt"]
SUBTYPES_DOM = ["rodinný dom", "rodinný dom", "chata/chalupa"]
SUBTYPES_POZEMOK = ["stavebný pozemok", "záhrada"]

CONDITIONS = ["pôvodný stav", "čiastočná rekonštrukcia", "po rekonštrukcii", "novostavba"]

SOURCES = [
    "https://www.nehnutelnosti.sk/listing/{}",
    "https://reality.bazos.sk/inzerat/{}",
    "https://www.topreality.sk/nehnutelnost/{}.html",
]

NITRA_DISTRICTS = ["Chrenová", "Zobor", "Klokočina", "Staré Mesto", "Mlynárce", "Čermáň"]


def random_area(ptype: str) -> float:
    if ptype == "byt":
        return round(random.uniform(35, 120), 1)
    elif ptype == "dom":
        return round(random.uniform(80, 280), 1)
    else:  # pozemok
        return round(random.uniform(300, 2000), 0)


def gen_listing_data(i: int) -> dict:
    city = random.choice(CITIES)
    ptype = random.choice(PTYPES)
    baseline = MARKET_BASELINES.get((city, ptype), 1_400)

    # Normal listings: ±30% variance around baseline
    variance = random.gauss(1.0, 0.15)
    price_per_sqm = max(300, baseline * variance)

    area = random_area(ptype)
    absolute_price = round(price_per_sqm * area / 1000) * 1000  # round to 1000

    # Subtype
    if ptype == "byt":
        subtype = random.choice(SUBTYPES_BYT)
    elif ptype == "dom":
        subtype = random.choice(SUBTYPES_DOM)
    else:
        subtype = random.choice(SUBTYPES_POZEMOK)

    condition = random.choice(CONDITIONS) if ptype != "pozemok" else None
    district = random.choice(NITRA_DISTRICTS) if city == "Nitra" else None
    source_url = random.choice(SOURCES).format(1000 + i)

    return dict(
        source_url=source_url,
        location_city=city,
        location_district=district,
        property_type=ptype,
        area_sqm=area,
        absolute_price=float(absolute_price),
        subtype=subtype,
        condition=condition,
    )


def inject_hot_deals(db, base_i: int) -> int:
    """
    Inject 5 explicitly underpriced listings to guarantee Hot Deals appear.
    These are ~35% below baseline.
    """
    hot_configs = [
        ("Nitra", "byt", 72.0, 2_400 * 0.60, "Nitra - Klokočina"),
        ("Nitra", "byt", 55.0, 2_400 * 0.62, "Nitra - Chrenová"),
        ("Nitra", "dom", 140.0, 1_900 * 0.63, None),
        ("Šurany", "byt", 65.0, 1_450 * 0.61, None),
        ("Zlaté Moravce", "dom", 110.0, 1_300 * 0.64, None),
    ]

    count = 0
    for idx, (city, ptype, area, ppsqm, district) in enumerate(hot_configs):
        absolute = round(ppsqm * area / 1000) * 1000
        raw = RawListing(
            source_url=f"https://www.nehnutelnosti.sk/hot-deal/{base_i + idx}",
            location_city=city,
            property_type=ptype,
            area_sqm=area,
            absolute_price=float(absolute),
            location_district=district,
            subtype="3-izbový byt" if ptype == "byt" else "rodinný dom",
            condition="pôvodný stav",
        )
        upsert_listing(db, raw)
        count += 1
    return count


def seed(n_normal: int = 120) -> None:
    init_db()
    db = SessionLocal()

    inserted = 0
    merged = 0

    logger.info("Seeding %d normal listings…", n_normal)
    for i in range(n_normal):
        data = gen_listing_data(i)
        raw = RawListing(**data)
        _, created = upsert_listing(db, raw)
        if created:
            inserted += 1
        else:
            merged += 1

    logger.info("Seeding hot deal listings…")
    hot_count = inject_hot_deals(db, n_normal)

    db.close()
    logger.info(
        "Done! Inserted=%d | Merged/deduped=%d | Hot deals injected=%d",
        inserted, merged, hot_count,
    )


if __name__ == "__main__":
    seed()
