"""Seed >=2 years of dev order/member history (DEV-ONLY, INSERT-only).

Adds deterministic backfill rows to a dev database so comparisons,
alive-curves and seasonality read meaningfully:

- 60 customers M-020001..M-020060 (M-02 block is free on dev), created_at
  spread 2024-10-01 -> 2026-09-15.
- Orders for those members for 2024-10..2025-09 plus a 2025-11 gap fill
  (dev has zero orders that month), ~2 orders/member/month with
  seasonal weights (Dec peak, Feb trough).
- external_order_id = DH-<member_idx>-<yyyymm>-<seq>, deterministic under
  RNG seed 42, so reruns are no-ops (existing ids are skipped in Python;
  dev orders predate the 014 UNIQUE so there is no DB constraint to lean on).

Existing rows are never touched: only INSERTs, new member block, new
external ids, real dev stores (CWB-01/TSM-02). Run:

    python scripts/seed_dev_history.py --database-url postgresql://...
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone

import asyncpg

SEED = 42
MEMBER_BLOCK = "M-02"
N_MEMBERS = 60
SOURCE_TAG = "DH2"  # v2: even daily spread (v1 DH series was bursty + never used days 29-31)

# (start, end) inclusive month ranges to generate: the pre-2025-10 year + Nov-2025 gap.
MONTH_RANGES = [("2024-10", "2025-09"), ("2025-11", "2025-11")]

SEASONALITY = {1: 0.9, 2: 0.7, 3: 1.0, 4: 1.0, 5: 1.1, 6: 1.0,
               7: 1.0, 8: 1.1, 9: 1.0, 10: 1.2, 11: 1.5, 12: 1.8}
BASE_ORDERS_PER_MONTH = 2.0

CORE_SKUS = ["FRM-001", "FRM-002", "FRM-003", "LENS-001", "LENS-002",
             "LENS-003", "SUN-001", "SUN-002", "ACC-001", "ACC-002",
             "ACC-003", "CON-001"]
STORE_CODES = ["CWB-01", "TSM-02"]


def months_between(start: str, end: str):
    y, m = int(start[:4]), int(start[5:])
    ey, em = int(end[:4]), int(end[5:])
    out = []
    while (y, m) <= (ey, em):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


TARGET_MONTHS = [mm for s, e in MONTH_RANGES for mm in months_between(s, e)]
# Partial-month fills: (year, month) -> (first_day, last_day) spread bounds.
# 2025-10-01..09 is a documented hole (legacy orders start Oct 10); filling
# only days 1-9 keeps seed and legacy ranges disjoint and feeds the October
# YoY day-aligned MTD window.
PARTIAL_MONTHS = {(2025, 10): (1, 9)}
TARGET_MONTHS.append((2025, 10))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    args = ap.parse_args()
    if not args.database_url:
        print("set --database-url (never default a dev seeder at a live DB)", file=sys.stderr)
        return 2

    rng = random.Random(SEED)
    conn = await asyncpg.connect(args.database_url)
    try:
        stores = {r["code"]: r["id"] for r in
                  await conn.fetch("SELECT id, code FROM stores WHERE code = ANY($1)", STORE_CODES)}
        missing = [c for c in STORE_CODES if c not in stores]
        if missing:
            print(f"missing dev stores: {missing}", file=sys.stderr)
            return 2
        products = await conn.fetch("SELECT id, sku, price_cents FROM products WHERE sku = ANY($1)", CORE_SKUS)
        pmap = {r["sku"]: dict(r) for r in products}
        missing = [s for s in CORE_SKUS if s not in pmap]
        if missing:
            print(f"missing dev products: {missing}", file=sys.stderr)
            return 2

        members = [f"{MEMBER_BLOCK}{i:04d}" for i in range(1, N_MEMBERS + 1)]
        have_members = {r["member_id"] for r in await conn.fetch(
            "SELECT member_id FROM customers WHERE member_id = ANY($1)", members)}
        new_members = [m for m in members if m not in have_members]
        join_start = date(2024, 10, 1)
        join_span = (date(2026, 9, 15) - join_start).days
        member_ids: dict[str, str] = {}
        for i, m in enumerate(members):
            if m in have_members:
                r = await conn.fetchrow("SELECT id FROM customers WHERE member_id=$1", m)
                member_ids[m] = str(r["id"])
                continue
            # Deterministic join date from index (not RNG stream) so reruns match.
            created = datetime(join_start.year, join_start.month, join_start.day,
                               tzinfo=timezone.utc) + timedelta(days=(i * join_span) // max(len(members) - 1, 1))
            idx = members.index(m)
            r = await conn.fetchrow(
                "INSERT INTO customers (member_id, full_name, created_at) VALUES ($1,$2,$3)"
                " RETURNING id", m, f"Dev History {idx + 1:02d}", created)
            member_ids[m] = str(r["id"])
        print(f"members: {len(members)} total, {len(new_members)} inserted")

        have_ext = {r["external_order_id"] for r in await conn.fetch(
            "SELECT external_order_id FROM orders WHERE external_order_id LIKE $1", f"{SOURCE_TAG}-%")}
        # Pass 1: order counts per (member, month). One rng.random() call per
        # cell in fixed order — the stream (hence n and ext ids) is identical
        # on reruns.
        counts: dict[tuple, int] = {}
        for mi in range(len(members)):
            for (y, mo) in TARGET_MONTHS:
                weight = SEASONALITY[mo]
                mu = BASE_ORDERS_PER_MONTH * weight
                n = int(mu) + (1 if rng.random() < mu % 1 else 0)
                if (y, mo) in PARTIAL_MONTHS:
                    lo, hi = PARTIAL_MONTHS[(y, mo)]
                    dim = calendar.monthrange(y, mo)[1]
                    n = round(n * (hi - lo + 1) / dim)
                counts[(mi, y, mo)] = n
        # Pass 2: spread each month's total evenly across its days (global
        # round-robin — per-member spacing alone clumps every member onto the
        # same lattice days). Times keep RNG variety; all draws precede the
        # skip check so reruns consume the identical stream (rerun = no-op).
        order_rows: list[tuple] = []
        seq: dict[tuple, int] = {}
        for (y, mo) in TARGET_MONTHS:
            dim = calendar.monthrange(y, mo)[1]
            lo, hi = PARTIAL_MONTHS.get((y, mo), (1, dim))
            span = hi - lo + 1
            cells = [(mi, k) for mi in range(len(members)) for k in range(counts[(mi, y, mo)])]
            for j, (mi, k) in enumerate(cells):
                m = members[mi]
                key = (mi, y, mo)
                seq[key] = seq.get(key, 0) + 1
                day = lo + (j * span) // max(len(cells), 1)
                ordered = datetime(y, mo, day, rng.randint(9, 20), rng.randint(0, 59), tzinfo=timezone.utc)
                store = stores[STORE_CODES[(mi + mo) % len(STORE_CODES)]]
                ext = f"{SOURCE_TAG}-{mi + 1:02d}-{y}{mo:02d}-{seq[key]:02d}"
                if ext in have_ext:
                    continue
                order_rows.append((member_ids[m], store, ordered, ext))

        # Items + totals, computed deterministically from the same RNG stream.
        payback: list[tuple] = []
        item_rows: list[tuple] = []  # (ext_id, sku, qty)
        for (cust_id, store_id, ordered, ext) in order_rows:
            lines = []
            total = 0
            for _ in range(rng.randint(1, 3)):
                sku = CORE_SKUS[rng.randint(0, len(CORE_SKUS) - 1)]
                qty = rng.randint(1, 3)
                total += pmap[sku]["price_cents"] * qty
                lines.append((ext, pmap[sku]["id"], qty, pmap[sku]["price_cents"]))
            payback.append((cust_id, store_id, ordered, total, ext))
            item_rows.extend(lines)

        if payback:
            await conn.executemany(
                "INSERT INTO orders (customer_id, store_id, ordered_at, total_cents, status, external_order_id)"
                " VALUES ($1,$2,$3,$4,'completed',$5)", payback)
            idmap = {r["external_order_id"]: str(r["id"]) for r in await conn.fetch(
                "SELECT id, external_order_id FROM orders WHERE external_order_id = ANY($1)",
                [p[4] for p in payback])}
            await conn.executemany(
                "INSERT INTO order_items (order_id, product_id, qty, unit_price_cents) VALUES ($1,$2,$3,$4)",
                [(idmap[ext], pid, qty, price) for (ext, pid, qty, price) in item_rows])
        span = await conn.fetchrow("SELECT min(ordered_at)::date lo, max(ordered_at)::date hi, count(*) n FROM orders")
        print(f"orders: {len(payback)} inserted ({len(item_rows)} lines); dev range now {span['lo']}..{span['hi']} ({span['n']} rows)")

        # DATA-1: every seed member gets phone + email contact rows so the
        # customer list, search and PII paths have full coverage. Deterministic
        # values; existing contacts are never touched.
        have_contact = {str(r["id"]) for r in await conn.fetch(
            "SELECT cu.id FROM customers cu JOIN customer_contacts cc ON cc.customer_id = cu.id"
            " WHERE cu.member_id = ANY($1)", members)}
        used_phones = {r["value_normalized"] for r in await conn.fetch(
            "SELECT value_normalized FROM customer_contacts WHERE kind='phone'")}
        contact_rows: list[tuple] = []
        for idx, m in enumerate(members, start=1):
            if member_ids[m] in have_contact:
                continue
            n = 90000000 + (idx * 7919) % 9999999
            phone = f"+852{n:08d}"
            while phone in used_phones:  # deterministic bump on collision
                n += 1
                phone = f"+852{n:08d}"
            used_phones.add(phone)
            disp = f"+852 {phone[4:8]} {phone[8:]}"
            email = f"devhistory{idx:02d}@example.com"
            contact_rows.append((member_ids[m], "phone", phone, disp, True))
            contact_rows.append((member_ids[m], "email", email, email, False))
        if contact_rows:
            await conn.executemany(
                "INSERT INTO customer_contacts (customer_id, kind, value_normalized, value_display, is_primary)"
                " VALUES ($1,$2,$3,$4,$5)", contact_rows)
        print(f"contacts: {len(contact_rows)} inserted for {len(contact_rows) // 2} members")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
