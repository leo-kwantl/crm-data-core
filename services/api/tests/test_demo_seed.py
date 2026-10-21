"""The demo seed is a no-op on a re-seed — including on a cluster that was
seeded with the pre-stable-keys file (data's `db/seeds/demo.sql`).

The seed gives its 15 demo orders stable keys (`source = 'demo'`,
`external_order_id = 'DEMO-O-0NN'`), so the partial unique index
`orders_source_ext_ux` (035) makes a re-seed a no-op: the insert's rows
conflict and are skipped, and the demo orders keep the timestamps of the
first seed.

A cluster seeded with the file BEFORE the stable keys carries the same 15
orders with those two columns NULL. The index's predicate is
`source IS NOT NULL`, so those rows are outside the index: nothing
conflicts, the insert ADDS 15 more demo orders, and the demo set doubles.
No reader could see it — nothing filters on `orders.source` (measured over
`db/queries`, `services/api`, `apps/web`: 0 hits) — so the seed's own
re-seed check is the only place it can be caught.

Every write here runs inside one transaction that is rolled back, so the
demo space is left exactly as the session found it (the suite's
session-finish row-count audit stays clean).
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SEED = REPO / "db" / "seeds" / "demo.sql"

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

SEED_SQL = SEED.read_text(encoding="utf-8")


@contextlib.asynccontextmanager
async def _conn(database_url: str) -> AsyncIterator:
    """A pool of this test's own: the session pool's connections are bound
    to the session's event loop, and a test runs in its own."""
    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            yield conn
    finally:
        await pool.close()


async def _apply_seed(conn) -> None:
    await conn.execute(SEED_SQL)


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


@pytest.mark.asyncio
async def test_a_fresh_seed_writes_the_demo_orders_with_stable_keys(database_url):
    async with _conn(database_url) as conn:
        async with conn.transaction():
            await _apply_seed(conn)
            rows = await _demo_orders(conn)
            assert len(rows) == DEMO_ORDERS
            assert all(r["source"] == "demo" for r in rows)
            assert {r["external_order_id"] for r in rows} == {
                f"DEMO-O-{i:03d}" for i in range(1, 16)
            }


@pytest.mark.asyncio
async def test_a_reseed_on_a_fresh_cluster_does_not_double_the_demo_orders(database_url):
    """The stable keys make the second seed a no-op."""
    async with _conn(database_url) as conn:
        async with conn.transaction():
            await _apply_seed(conn)
            await _apply_seed(conn)
            rows = await _demo_orders(conn)
            assert len(rows) == DEMO_ORDERS
            assert await _demo_item_count(conn) == DEMO_ORDERS


@pytest.mark.asyncio
async def test_a_reseed_on_a_legacy_cluster_does_not_double_the_demo_orders(database_url):
    """The cluster was seeded with the file BEFORE the stable keys.

    Its 15 demo orders carry `source`/`external_order_id` NULL, and the
    partial unique index does not cover them, so without the seed's
    backfill the re-seed would ADD 15 more demo orders.
    """
    async with _conn(database_url) as conn:
        async with conn.transaction():
            await _apply_seed(conn)
            # The legacy shape: the same orders, no stable keys.
            await conn.execute(
                "UPDATE orders SET source = NULL, external_order_id = NULL "
                "WHERE source = 'demo'"
            )
            legacy = await _demo_orders(conn)
            assert len(legacy) == DEMO_ORDERS
            assert all(r["source"] is None for r in legacy)

            await _apply_seed(conn)

            rows = await _demo_orders(conn)
            assert len(rows) == DEMO_ORDERS, (
                "the re-seed doubled the demo orders: the legacy rows carry "
                "no stable key, so the insert did not conflict"
            )
            assert all(r["source"] == "demo" for r in rows)
            assert len({r["external_order_id"] for r in rows}) == DEMO_ORDERS
            assert await _demo_item_count(conn) == DEMO_ORDERS
