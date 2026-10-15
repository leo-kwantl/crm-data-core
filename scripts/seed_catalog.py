"""Catalog seeder: realistic eyewear catalog for the pilot DB (DEV-ONLY).

Owner-directed reseed follow-up 2026-10-15 ("data should be more random
yet follow real-world patterns"): the reseeded DB had only the demo
catalog (12 products / 2 stores / 1 brand) against 1,208 customers and
9,372 orders, and the app's reports showed it as thin.

Additive + idempotent: INSERT-only with deterministic UUIDs (uuid5), so
re-runs and re-bootstraps are no-ops. Nothing existing is touched or
deleted — the 12 demo SKUs and 2 demo stores survive so the 15 demo
orders keep valid FKs. Resulting totals land in the requested bands:

  products     12 demo + 56 seeded = 68   (asked 40-80)
  stores        2 demo +  8 seeded = 10   (asked 6-12)
  brands        1 demo +  5 seeded =  6   (asked 4-6)

Brands: 5 real-sounding HK optical groups, each owning 1-2 stores (no
0-store brand). Stores: 8 across HK districts; `stores` has no district
column (006 added address/phone; 011 added brand_id), so the district
lives in the name + address line.

Prices are per-category log-normal (median/sigma below), rounded to
$5 — the spread is the point, not any single tier.

Run (order matters: after bootstrap and BEFORE `seed_reseed_1200.py`
-- the reseeder's campaign-member slice attaches to these campaigns
and its baskets draw on these 68 SKUs, so running it afterwards leaves
those campaigns silently at the handful of members seeded here;
rerun-safe either way):
    python scripts/seed_catalog.py --database-url postgresql://...

NOTE: app test fixtures write brands/campaigns into a shared DB and are
NOT rolled back (their half — see the room report); this script does
not prune them.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import sys
import uuid
from datetime import date

import asyncpg

SEED = 11
NS = uuid.uuid5(uuid.NAMESPACE_DNS, "crm-cdp-data.seed_catalog")


def _id(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(NS, f"{kind}:{key}")


# --- brands: 5 real-sounding HK optical groups -------------------------
BRANDS = [
    ("SUNHING", "Sun Hing Optical Group"),
    ("KVLN", "Kowloon Vision Holdings"),
    ("VICEW", "Victoria Eyewear Company"),
    ("HARBOR", "Harbour Optics Group"),
    ("PEAKLENS", "Peak Lens Studio"),
]

# --- stores: 8 across HK districts (name carries the district) ---------
STORES = [
    ("CEN-11", "Sun Hing Central Flagship", "Central",
     "Shop 1103, 11/F, 8 Queen's Road Central", "+852 2111 0110",
     "SUNHING"),
    ("MK-12", "Sun Hing Mong Kok", "Mong Kok",
     "Shop 210, Grand Plaza, 625 Nathan Road", "+852 2111 0120",
     "SUNHING"),
    ("SSP-13", "Kowloon Vision Sham Shui Po", "Sham Shui Po",
     "Shop G12, 270 Fuk Wing Street", "+852 2111 0130", "KVLN"),
    ("WTS-14", "Kowloon Vision Wong Tai Sin", "Wong Tai Sin",
     "Shop 105, Temple Mall North, 2 Lung Cheung Road", "+852 2111 0140",
     "KVLN"),
    ("WC-15", "Victoria Eyewear Wan Chai", "Wan Chai",
     "Shop B, 128 Hennessy Road", "+852 2111 0150", "VICEW"),
    ("TKO-16", "Victoria Eyewear Tseung Kwan O", "Tseung Kwan O",
     "Shop 216, Park Central, 9 Tong Tak Street", "+852 2111 0160",
     "VICEW"),
    ("TST-17", "Harbour Optics East Tsim Sha Tsui", "Tsim Sha Tsui",
     "Shop 3, 39 Granville Road", "+852 2111 0170", "HARBOR"),
    ("ST-18", "Peak Lens Studio Sha Tin", "Sha Tin",
     "Shop 508, New Town Plaza Phase 1", "+852 2111 0180", "PEAKLENS"),
]

# --- products: per-category name bank + log-normal price shape ---------
# (median_cents, sigma) — rounded to $5. Category weights mirror the
# basket mix in seed_reseed_1200 (care/accessories frequent, frames rare).
CAT_PRICE = {
    "frames": (128000, 0.40),
    "lenses": (68000, 0.45),
    "sunglasses": (168000, 0.40),
    "accessories": (11000, 0.55),
    "care": (8500, 0.40),
}
CAT_SKU = {"frames": "FRM", "lenses": "LENS", "sunglasses": "SUN",
           "accessories": "ACC", "care": "CON"}

FRAME_NAMES = [
    "Audrey Acetate Round", "Bauhinia Titanium Rectangle",
    "Cascade Metal Browline", "Verity Rimless Oval",
    "Harbour Round Metal", "Lion Rock Acetate Square",
    "Kai Tak Blue-Light Rectangle", "Star Ferry Round Tortoise",
    "Peak Tram Titanium Rimless", "Nathan Road Classic Rectangle",
    "Mid-Levels Acetate Cat-Eye", "Jade Market Round Metal",
    "Tramline Half-Rim Rectangle", "Aqua Luna Acetate Oval",
    "Repulse Bay Rimless Rectangle", "Lantern Lane Round Acetate",
    "Dragon Centre Slim Rectangle", "Ngong Ping Lightweight Oval",
]
LENS_NAMES = [
    "1.56 Standard Single-Vision Lenses", "1.61 Aspheric Lenses",
    "1.67 High-Index Lenses", "1.74 Ultra-Thin High-Index Lenses",
    "Blue-Light Filter Lenses 1.60", "Photochromic Grey 1.56",
    "Photochromic Brown 1.67", "Polarised Grey Lenses 1.60",
    "Drive-Safe Night Lenses", "Progressive Short Corridor Lenses",
    "Progressive Wide Corridor Lenses", "Office PC Blue-Light Lenses",
]
SUN_NAMES = [
    "Aviator Gold Polarised Sunglasses", "Wayfarer Matte Black Sunglasses",
    "Cat-Eye Acetate Sunglasses", "Round Metal Retro Sunglasses",
    "Sport Wrap TR90 Sunglasses", "Harbour Polarised Driving Sunglasses",
    "Folding Travel Polarised Sunglasses", "Oversized Gradient Sunglasses",
    "Kids' Flexible Rubber Sunglasses", "Bamboo Temple Clubmaster",
    "Alpine Mirror Ski Sunglasses", "Vintage Round Gradient Sunglasses",
]
ACC_NAMES = [
    "Leather Hard Case", "Foldable Leather Case",
    "Microfiber Cloth 3-pack", "Anti-Fog Spray 60ml",
    "Eyewear Screwdriver Kit", "Temple Tip Kit (Pair)",
    "Clip-On Sun Lenses", "Glasses Strap Cord",
    "Lens Wipes 50-pack", "Ultrasonic Cleaning Voucher",
]
CARE_NAMES = [
    "Contact Lens Solution 360ml",
    "Daily Disposable Contacts (30-pack)",
    "Monthly Silicone Hydrogel Contacts (6-pack)",
    "Contact Lens Wetting Drops",
]

NAMES = {"frames": FRAME_NAMES, "lenses": LENS_NAMES,
         "sunglasses": SUN_NAMES, "accessories": ACC_NAMES,
         "care": CARE_NAMES}

COLORS = ["tortoise", "matte black", "gunmetal", "champagne", "navy",
          "burgundy", "crystal clear", "rose gold", "olive", "slate"]
MATERIALS = {"frames": ["acetate", "titanium", "TR90", "stainless steel"],
             "sunglasses": ["acetate", "metal", "TR90", "nylon"],
             "lenses": ["CR-39", "polycarbonate", "MR-8"],
             "accessories": ["leather", "microfibre", "plastic"],
             "care": ["solution"]}

# --- campaigns: 6 realistic HK retail voices, brand-scoped -------------
CAMPAIGNS = [
    ("SUMMER-FRAME-FIESTA", "Summer Frame Fiesta", "active", 15,
     "SUNHING", "2026-06-01", "2026-08-31"),
    ("DOUBLE-11-LENS-UPGRADE", "Double 11 Lens Upgrade", "active", 20,
     "KVLN", "2026-11-01", "2026-11-15"),
    ("MTR-COMMUTER-PRESCRIPTION", "MTR Commuter Prescription Deal",
     "active", 10, "HARBOR", "2026-09-01", "2026-12-31"),
    ("BACK-TO-SCHOOL-KIDS-EYEWEAR", "Back-to-School Kids' Eyewear",
     "draft", 25, "PEAKLENS", None, None),
    ("GOLDEN-WEEK-SUNGLASS-EDIT", "Golden Week Sunglass Edit", "ended",
     12, "VICEW", "2026-09-28", "2026-10-08"),
    ("VIP-EYE-EXAM-FRAME-BUNDLE", "VIP Eye-Exam & Frame Bundle",
     "active", 18, "VICEW", "2026-10-01", "2027-03-31"),
]


def _date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _price(rng: random.Random, category: str) -> int:
    median, sigma = CAT_PRICE[category]
    raw = math.exp(math.log(median) + rng.gauss(0.0, sigma))
    return int(max(round(raw / 500.0) * 500, 1000))


def _descriptions(rng: random.Random, category: str) -> tuple[str, str]:
    color = rng.choice(COLORS)
    material = rng.choice(MATERIALS[category])
    if category in ("frames", "sunglasses"):
        specs = (f'{{"lens_width": {rng.randint(46, 58)}, '
                 f'"bridge": {rng.randint(14, 22)}, '
                 f'"temple": {rng.randint(130, 150)}, '
                 f'"color": "{color}", "material": "{material}"}}')
        what = "frame" if category == "frames" else "sunglasses"
        desc = (f"Everyday {what} in {material} with a {color} finish. "
                "Adjustable nose pads and spring hinges for all-day wear.")
    elif category == "lenses":
        specs = (f'{{"index": "{color}", "material": "{material}", '
                 f'"coating": "{rng.choice(['AR', 'hard', 'hydrophobic'])}"}}')
        desc = ("Precision-cut prescription lenses with scratch-resistant "
                "anti-reflective coating; fits standard and slim frames.")
    elif category == "accessories":
        specs = f'{{"material": "{material}", "color": "{color}"}}'
        desc = "Countertop accessory, stocked at every branch."
    else:
        specs = '{"volume_ml": 360}'
        desc = "Contact-lens care essential, stocked at every branch."
    return desc, specs


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url",
                    default=os.environ.get("DATABASE_URL", ""))
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    if not args.database_url:
        print("set --database-url (never default a seeder at a live DB)",
              file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    conn = await asyncpg.connect(args.database_url)
    try:
        group = await conn.fetchval(
            "SELECT id FROM groups WHERE code = 'DEMO'")
        if group is None:
            print("no DEMO group: run bootstrap first", file=sys.stderr)
            return 2
        group = str(group)

        # brands
        brand_rows = [(str(_id("brand", code)), group, code, name)
                      for code, name in BRANDS]
        await conn.executemany(
            "INSERT INTO brands (id, group_id, code, name)"
            " VALUES ($1::uuid, $2::uuid, $3, $4)"
            " ON CONFLICT (id) DO NOTHING", brand_rows)
        bmap = {code: str(_id("brand", code)) for code, _ in BRANDS}

        # stores (district only exists in name/address — no column)
        store_rows = [(str(_id("store", code)), code, name, bmap[brand],
                       address, phone)
                      for (code, name, _d, address, phone, brand) in STORES]
        await conn.executemany(
            "INSERT INTO stores (id, code, name, brand_id, address, phone)"
            " VALUES ($1::uuid, $2, $3, $4::uuid, $5, $6)"
            " ON CONFLICT (id) DO NOTHING", store_rows)

        # products (log-normal price per category) + 008 enrichment
        prod_rows, enrich_rows = [], []
        for category, names in NAMES.items():
            prefix = CAT_SKU[category]
            for i, name in enumerate(names, start=101):
                sku = f"{prefix}-{i}"
                pid = str(_id("product", sku))
                prod_rows.append((pid, sku, name, category,
                                  _price(rng, category)))
                desc, specs = _descriptions(rng, category)
                enrich_rows.append((desc, specs, pid))
        await conn.executemany(
            "INSERT INTO products (id, sku, name, category, price_cents)"
            " VALUES ($1::uuid, $2, $3, $4, $5)"
            " ON CONFLICT (id) DO NOTHING", prod_rows)
        await conn.executemany(
            "UPDATE products SET description = $1, specs = $2::jsonb,"
            " image_url = 'https://picsum.photos/seed/' || sku || '/600/400'"
            " WHERE id = $3::uuid AND description IS NULL",
            enrich_rows)

        # campaigns (brand-scoped so brand-scoped sa sessions can see them)
        camp_rows = [(str(_id("campaign", code)), code, name, "promotion",
                      status, bmap[brand], f'{{"discount_pct": {pct}}}',
                      _date(starts), _date(ends))
                     for (code, name, status, pct, brand, starts, ends)
                     in CAMPAIGNS]
        await conn.executemany(
            "INSERT INTO campaigns (id, code, name, kind, status, brand_id,"
            " config, starts_at, ends_at)"
            " VALUES ($1::uuid, $2, $3, $4, $5, $6::uuid, $7::jsonb,"
            " $8::date, $9::date) ON CONFLICT (id) DO NOTHING", camp_rows)

        # a few campaign members so the campaign page isn't empty
        active = [(str(_id("campaign", c[0])), c[0]) for c in CAMPAIGNS
                  if c[2] == "active"]
        tops = await conn.fetch(
            "SELECT id FROM customers ORDER BY id LIMIT 15")
        member_rows = []
        for cid, code in active:
            for r in tops:
                member_rows.append((cid, r["id"], "seed"))
        await conn.executemany(
            "INSERT INTO campaign_members (campaign_id, customer_id,"
            " assigned_by) VALUES ($1::uuid, $2::uuid, $3)"
            " ON CONFLICT (campaign_id, customer_id) DO NOTHING",
            member_rows)

        # report — counts + the price spread that was the point
        for tbl in ("brands", "stores", "products", "campaigns"):
            n = await conn.fetchval(f"SELECT count(*) FROM {tbl}")
            print(f"{tbl}={n}")
        zero_store = await conn.fetchval(
            "SELECT count(*) FROM brands b WHERE NOT EXISTS"
            " (SELECT 1 FROM stores s WHERE s.brand_id = b.id)")
        spread = await conn.fetchrow(
            "SELECT count(*) n, min(price_cents) lo,"
            " percentile_disc(0.5) WITHIN GROUP (ORDER BY price_cents) p50,"
            " max(price_cents) hi FROM products")
        print(f"zero_store_brands={zero_store} products_spread="
              f"n{spread['n']} {spread['lo']}c/{spread['p50']}c/"
              f"{spread['hi']}c")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
