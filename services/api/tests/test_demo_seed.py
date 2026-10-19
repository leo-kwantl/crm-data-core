"""The demo seed must converge: a second run inserts no rows.

`scripts/bootstrap_db.py` wipes the demo tables before seeding, but the seed
itself has to be safe to re-run on a cluster where a TRUNCATE is not allowed
(a live DB is migrate-only). Orders and order_items were the two tables
without a stable key, so every re-seed duplicated all 15 orders and their
lines; they now carry `source='demo'` + `external_order_id='DEMO-O-NNN'`
(hitting `orders_source_ext_ux`) and the item insert is gated on the order
having no lines yet.

Both runs happen inside one transaction that is rolled back: this test file
shares the session database with the rest, and the demo rows would collide
with the member_ids other suites seed (M-000101..M-000108).
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[3]
TABLES = ("orders", "order_items", "customers", "products", "stores")


async def test_demo_seed_reseed_is_a_noop(database_url: str) -> None:
    seed = (REPO / "db" / "seeds" / "demo.sql").read_text(encoding="utf-8")
    conn = await asyncpg.connect(database_url)
    try:
        tx = conn.transaction()
        await tx.start()
        await conn.execute(seed)
        first = {t: int(await conn.fetchval(f"SELECT count(*) FROM {t}"))
                 for t in TABLES}
        await conn.execute(seed)
        second = {t: int(await conn.fetchval(f"SELECT count(*) FROM {t}"))
                  for t in TABLES}
        dup = await conn.fetchval(
            "SELECT count(*) FROM (SELECT source, external_order_id"
            " FROM orders WHERE source = 'demo'"
            " GROUP BY 1, 2 HAVING count(*) > 1) d")
        await tx.rollback()
    finally:
        await conn.close()
    assert first["orders"] >= 15 and first["order_items"] >= 15
    assert int(dup) == 0, "re-seed duplicated demo orders"
    assert second == first, f"re-seed changed counts: {first} -> {second}"
