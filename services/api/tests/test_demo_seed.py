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

The legacy case (a cluster seeded BEFORE the stable keys) is the one the
stable key alone does not cover: those 15 orders carry `source`/
`external_order_id` NULL, and `orders_source_ext_ux` is partial
(`source IS NOT NULL`), so nothing conflicts and the re-seed would ADD 15
more demo orders. The seed's backfill gives those rows the same stable key
first, so the re-seed is a no-op there too.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[3]
TABLES = ("orders", "order_items", "customers", "products", "stores")

# The eight demo customers the seed creates. The 15 demo orders are unique
# on (customer, amount), which is what the seed's backfill matches on.
DEMO_CUSTOMERS = [
    "c0000000-0000-0000-0000-000000000001",
    "c0000000-0000-0000-0000-000000000002",
    "c0000000-0000-0000-0000-000000000003",
    "c0000000-0000-0000-0000-000000000004",
    "c0000000-0000-0000-0000-000000000005",
    "c0000000-0000-0000-0000-000000000006",
    "c0000000-0000-0000-0000-000000000007",
    "c0000000-0000-0000-0000-000000000008",
]
DEMO_ORDERS = 15


async def _seed(conn) -> None:
    await conn.execute(
        (REPO / "db" / "seeds" / "demo.sql").read_text(encoding="utf-8"))


async def _demo_orders(conn) -> list:
    return await conn.fetch(
        "SELECT id, source, external_order_id FROM orders "
        "WHERE customer_id = ANY($1::uuid[]) "
        "ORDER BY external_order_id NULLS FIRST",
        DEMO_CUSTOMERS,
    )


async def _demo_item_count(conn) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM order_items oi JOIN orders o ON o.id = oi.order_id "
        "WHERE o.customer_id = ANY($1::uuid[])",
        DEMO_CUSTOMERS,
    )


async def test_demo_seed_reseed_is_a_noop(database_url: str) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        tx = conn.transaction()
        await tx.start()
        await _seed(conn)
        first = {t: int(await conn.fetchval(f"SELECT count(*) FROM {t}"))
                 for t in TABLES}
        await _seed(conn)
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


async def test_a_fresh_seed_writes_the_demo_orders_with_stable_keys(
        database_url: str) -> None:
    """The stable key is what makes the re-seed a no-op: it is the insert's
    conflict target (`orders_source_ext_ux`), and it is what a reader can
    see."""
    conn = await asyncpg.connect(database_url)
    try:
        tx = conn.transaction()
        await tx.start()
        await _seed(conn)
        rows = await _demo_orders(conn)
        await tx.rollback()
    finally:
        await conn.close()
    assert len(rows) == DEMO_ORDERS
    assert all(r["source"] == "demo" for r in rows)
    assert {r["external_order_id"] for r in rows} == {
        f"DEMO-O-{i:03d}" for i in range(1, 16)}


async def test_a_reseed_on_a_legacy_cluster_does_not_double_the_demo_orders(
        database_url: str) -> None:
    """The cluster was seeded with the file BEFORE the stable keys.

    Its 15 demo orders carry `source`/`external_order_id` NULL, and the
    partial unique index does not cover them, so without the seed's
    backfill the re-seed would ADD 15 more demo orders — and nothing
    filters on `orders.source`, so no reader could see the difference.
    """
    conn = await asyncpg.connect(database_url)
    try:
        tx = conn.transaction()
        await tx.start()
        await _seed(conn)
        # The legacy shape: the same orders, no stable keys.
        await conn.execute(
            "UPDATE orders SET source = NULL, external_order_id = NULL "
            "WHERE source = 'demo'")
        legacy = await _demo_orders(conn)
        await _seed(conn)
        rows = await _demo_orders(conn)
        items = await _demo_item_count(conn)
        await tx.rollback()
    finally:
        await conn.close()
    assert len(legacy) == DEMO_ORDERS
    assert all(r["source"] is None for r in legacy)
    assert len(rows) == DEMO_ORDERS, (
        "the re-seed doubled the demo orders: the legacy rows carry no "
        "stable key, so the insert did not conflict")
    assert all(r["source"] == "demo" for r in rows)
    assert len({r["external_order_id"] for r in rows}) == DEMO_ORDERS
    assert items == DEMO_ORDERS


async def test_a_reseed_on_a_legacy_double_seed_neither_fails_nor_grows(
        database_url: str) -> None:
    """The legacy seed was not re-seed safe, so a cluster whose legacy seed
    ran twice holds 30 demo orders (15 keyed by the backfill, 15 copies
    without a key).

    Keying all 30 would violate `orders_source_ext_ux` — the seed would
    fail on exactly the cluster it is meant to repair — and the re-seed
    must add nothing on top of the 30.
    """
    conn = await asyncpg.connect(database_url)
    try:
        tx = conn.transaction()
        await tx.start()
        await _seed(conn)
        await conn.execute(
            "UPDATE orders SET source = NULL, external_order_id = NULL "
            "WHERE source = 'demo'")
        # The legacy seed's second run: the same orders, inserted again.
        await conn.execute(
            "INSERT INTO orders (customer_id, store_id, ordered_at, "
            "total_cents, status) SELECT customer_id, store_id, ordered_at, "
            "total_cents, status FROM orders WHERE source IS NULL "
            "AND customer_id = ANY($1::uuid[])",
            DEMO_CUSTOMERS)
        before = await _demo_orders(conn)
        assert len(before) == 2 * DEMO_ORDERS

        await _seed(conn)

        after = await _demo_orders(conn)
        keyed = [r for r in after if r["source"] == "demo"]
        await tx.rollback()
    finally:
        await conn.close()
    assert len(after) == 2 * DEMO_ORDERS, (
        "the re-seed added orders on a legacy double seed: "
        f"{len(before)} -> {len(after)}")
    assert len(keyed) == DEMO_ORDERS
    assert len({r["external_order_id"] for r in keyed}) == DEMO_ORDERS
