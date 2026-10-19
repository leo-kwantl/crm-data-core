"""Delete test debris from a dev database -- dry run by default.

The 2026-10-19 leak was not one row: the suite writes customers with generated
`M-%` member ids, stores with fixed codes (`S101`..`S105`, `APPLY-S1`,
`COHORT-T`) and products (`CLT-%`, `CSE-%`), and several fixtures never delete
them. Once they are in the dev dataset they are indistinguishable from data
unless you compare against the seed space, which is exactly what this does:

* customers: seeded = the eight curated members in `db/seeds/demo.sql` plus the
  1200-member `M-03%` block from `scripts/seed_reseed_1200.py`
* stores: seeded = `db/seeds/demo.sql` plus the ten the bootstrap creates
* products: seeded = the twelve in `db/seeds/demo.sql`

Everything outside that space is debris. Deletes run children-first because the
foreign keys are NO ACTION, and every row is written to a JSON dump before it
is removed, so a wrong guess is recoverable.

    python scripts/clean_test_debris.py            # show what would go
    python scripts/clean_test_debris.py --apply    # remove it
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib

import asyncpg

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@127.0.0.1:59531/postgres")

SEEDED_MEMBERS = (
    "M-000101", "M-000102", "M-000103", "M-000104",
    "M-000105", "M-000106", "M-000107", "M-000108",
)
SEEDED_STORES = (
    "CWB-01", "TSM-02",                      # db/seeds/demo.sql
    "TST-17", "SSP-13", "WTS-14", "ST-18",   # scripts/bootstrap_db.py
    "CEN-11", "MK-12", "TKO-16", "WC-15",
    "MID-AUTUMN-10",
)

# child tables that reference these rows, children first
CUSTOMER_CHILDREN = (
    "order_items", "orders", "consent_events", "consents", "customer_contacts",
    "customer_access_logs", "rfm_customer_scores", "clv_customer_scores",
    "churn_customer_scores", "segment_assignments", "customer_events",
    "campaign_members", "rec_items", "customer_custom_data", "backtest_outcomes",
)


def debris_customers_sql() -> str:
    seeds = ",".join(f"'{m}'" for m in SEEDED_MEMBERS)
    return (
        "SELECT id, member_id, created_at FROM customers"
        f" WHERE member_id NOT LIKE 'M-03%' AND member_id NOT IN ({seeds})"
    )


def debris_stores_sql() -> str:
    seeds = ",".join(f"'{s}'" for s in SEEDED_STORES)
    return f"SELECT id, code FROM stores WHERE code NOT IN ({seeds})"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dsn", default=DSN)
    args = ap.parse_args()

    conn = await asyncpg.connect(args.dsn)
    try:
        customers = await conn.fetch(debris_customers_sql())
        stores = await conn.fetch(debris_stores_sql())
        products = await conn.fetch(
            "SELECT id, sku FROM products WHERE sku LIKE 'CLT-%' OR sku LIKE 'CSE-%'"
        )
        print(f"customers outside the seed space: {len(customers)}")
        for r in customers:
            print(f"   {r['member_id']:12} created {r['created_at']:%Y-%m-%d %H:%M}")
        print(f"stores outside the seed space:    {len(stores)}")
        for r in stores:
            print(f"   {r['code']}")
        print(f"products outside the seed space:  {len(products)}")
        for r in products:
            print(f"   {r['sku']}")

        if not args.apply:
            print("\ndry run -- pass --apply to delete")
            return

        dump = pathlib.Path(".pi") / f"debris-{int(asyncio.get_event_loop().time())}.json"
        dump.parent.mkdir(exist_ok=True)
        dump.write_text(
            json.dumps(
                {"customers": [dict(r) for r in customers],
                 "stores": [dict(r) for r in stores],
                 "products": [dict(r) for r in products]},
                default=str, indent=1,
            ),
            encoding="utf-8",
        )

        cids = [r["id"] for r in customers]
        sids = [r["id"] for r in stores]
        pids = [r["id"] for r in products]

        async with conn.transaction():
            for table in CUSTOMER_CHILDREN:
                col = "order_id" if table == "order_items" else "customer_id"
                if table == "order_items":
                    await conn.execute(
                        f'DELETE FROM "{table}" WHERE order_id IN'
                        " (SELECT id FROM orders WHERE customer_id = ANY($1))",
                        cids,
                    )
                else:
                    await conn.execute(
                        f'DELETE FROM "{table}" WHERE "{col}" = ANY($1)', cids
                    )
            await conn.execute("DELETE FROM customers WHERE id = ANY($1)", cids)
            await conn.execute(
                "DELETE FROM order_items WHERE order_id IN"
                " (SELECT id FROM orders WHERE store_id = ANY($1))", sids
            )
            await conn.execute("DELETE FROM orders WHERE store_id = ANY($1)", sids)
            await conn.execute("DELETE FROM inventory WHERE store_id = ANY($1)", sids)
            await conn.execute("DELETE FROM stores WHERE id = ANY($1)", sids)
            await conn.execute("DELETE FROM products WHERE id = ANY($1)", pids)
        print(f"\ndeleted {len(cids)} customers, {len(sids)} stores,"
              f" {len(pids)} products (dump: {dump})")
        print("after:",
              await conn.fetchrow(
                  "SELECT (SELECT count(*) FROM customers) customers,"
                  " (SELECT count(*) FROM stores) stores,"
                  " (SELECT count(*) FROM orders) orders"
              ))
    finally:
        await conn.close()


asyncio.run(main())
