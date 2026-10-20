"""Cohort money: `retained_cents` is what the cohort brought back.

The cohort table counted the customers a cohort brought back but not the
money, so the reader had no column to divide by for ARPU. The writer sums
the completed orders of each period, in integer cents.

This file writes into its OWN brand, because the cohort of a customer is
computed over every order of the brand: a shared database means another
test's order would land in this file's numbers otherwise.

Two things are held here:

- the money is the period's money (k=0 the cohort's first period, k=1 the
  next), summed from the completed orders of that period;
- a row that predates the migration reads 0 cents, which means "not
  measured" - never a NULL, never a "$0.00 that looks measured".
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Iterator

import pytest

from pipelines.snapshot import COHORT_SNAPSHOT_UPSERT_SQL, COHORT_TREND_SQL

GROUP_ID = "c0c00000-0000-0000-0000-0000000000a1"
BRAND_ID = "b0b00000-0000-0000-0000-0000000000b1"
STORE_CODE = "COHORTM-S1"
CUSTOMER_ID = "M-900201"

# (ordered_at, total_cents) - two in the cohort month, one in the next.
# The rows are re-seedable: the source + external id are this file's own, so
# a second test in this module re-runs the seed without doubling the money.
ORDERS = (
    ("2026-01-05T10:00:00+00:00", 1000, "CM-1"),
    ("2026-01-20T10:00:00+00:00", 500, "CM-2"),
    ("2026-02-10T10:00:00+00:00", 700, "CM-3"),
)


async def _purge(dsn: str) -> None:
    """Delete this file's rows: the cohort grain, the orders and the brand."""
    import asyncpg

    pool = await asyncpg.create_pool(dsn)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM cohort_snapshots WHERE brand_id = $1::uuid",
                BRAND_ID)
            await conn.execute(
                "DELETE FROM orders WHERE store_id IN"
                " (SELECT id FROM stores WHERE code = $1)", STORE_CODE)
            await conn.execute(
                "DELETE FROM customers WHERE member_id = $1", CUSTOMER_ID)
            await conn.execute(
                "DELETE FROM stores WHERE code = $1", STORE_CODE)
            await conn.execute("DELETE FROM brands WHERE id = $1::uuid", BRAND_ID)
            await conn.execute("DELETE FROM groups WHERE id = $1::uuid", GROUP_ID)
    finally:
        await pool.close()


@pytest.fixture(scope="module", autouse=True)
def _no_residue(database_url: str) -> Iterator[None]:
    started = datetime.now(timezone.utc)
    yield
    asyncio.run(_purge(database_url))


async def _seed(conn) -> None:  # type: ignore[no-untyped-def]
    """A brand of this file's own, a store, a customer and three orders."""
    await conn.execute(
        "INSERT INTO groups (id, code, name) VALUES ($1::uuid, 'COHORTM-GRP',"
        " 'Cohort Money Group') ON CONFLICT (id) DO NOTHING", GROUP_ID)
    await conn.execute(
        "INSERT INTO brands (id, group_id, code, name)"
        " VALUES ($1::uuid, $2::uuid, 'COHORTM-BRND', 'Cohort Money Brand')"
        " ON CONFLICT (id) DO NOTHING", BRAND_ID, GROUP_ID)
    await conn.execute(
        "INSERT INTO stores (code, name, brand_id)"
        " VALUES ($1, 'Cohort Money Store', $2::uuid)"
        " ON CONFLICT (code) DO NOTHING", STORE_CODE, BRAND_ID)
    await conn.execute(
        "INSERT INTO customers (member_id, full_name)"
        " VALUES ($1, 'Cohort Money Buyer') ON CONFLICT (member_id) DO NOTHING",
        CUSTOMER_ID)
    customer_id = await conn.fetchval(
        "SELECT id FROM customers WHERE member_id = $1", CUSTOMER_ID)
    store_id = await conn.fetchval(
        "SELECT id FROM stores WHERE code = $1", STORE_CODE)
    for ordered_at, total, external_id in ORDERS:
        await conn.execute(
            "INSERT INTO orders (customer_id, store_id, ordered_at,"
            " total_cents, status, source, external_order_id)"
            " VALUES ($1, $2, $3, $4, 'completed', 'cohortm-test', $5)"
            " ON CONFLICT DO NOTHING", customer_id, store_id,
            datetime.fromisoformat(ordered_at), total, external_id)


@pytest.mark.asyncio
async def test_the_writer_sums_the_money_of_each_period(
        database_url: str) -> None:
    """k=0 is the cohort's first period, k=1 the next; each carries its money."""
    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            await _seed(conn)
            await conn.execute(COHORT_SNAPSHOT_UPSERT_SQL, "month", 2,
                               BRAND_ID, None)
            rows = await conn.fetch(
                "SELECT period_offset, cohort_size, retained, retained_cents"
                " FROM cohort_snapshots WHERE granularity = 'month'"
                " AND brand_id = $1::uuid ORDER BY period_offset", BRAND_ID)
    finally:
        await pool.close()

    assert len(rows) == 3, rows
    by_offset = {int(r["period_offset"]): r for r in rows}
    assert int(by_offset[0]["cohort_size"]) == 1
    assert int(by_offset[0]["retained"]) == 1
    assert int(by_offset[0]["retained_cents"]) == 1500, by_offset[0]
    assert int(by_offset[1]["retained"]) == 1
    assert int(by_offset[1]["retained_cents"]) == 700, by_offset[1]
    # A period with no order retains nobody and no money: 0, not NULL.
    assert int(by_offset[2]["retained"]) == 0
    assert int(by_offset[2]["retained_cents"]) == 0, by_offset[2]


@pytest.mark.asyncio
async def test_the_trend_statement_carries_the_money(
        database_url: str) -> None:
    """The reader's ARPU divides the trend's money by its customers."""
    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            await _seed(conn)
            await conn.execute(COHORT_SNAPSHOT_UPSERT_SQL, "month", 1,
                               BRAND_ID, None)
            row = await conn.fetchrow(COHORT_TREND_SQL, "month", BRAND_ID,
                                      None)
    finally:
        await pool.close()

    assert row is not None
    assert int(row["retained"]) == 1
    assert int(row["retained_cents"]) == 1500
    # ARPU = retained_cents / retained, in cents: 1500 cents per customer.
    assert int(row["retained_cents"]) // int(row["retained"]) == 1500


@pytest.mark.asyncio
async def test_a_grain_written_before_the_migration_reads_zero_not_null(
        database_url: str) -> None:
    """0 means "not measured"; a NULL would be a second kind of nothing."""
    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO cohort_snapshots (granularity, cohort_period,"
                " period_offset, cohort_size, retained, retention_rate,"
                " brand_id) VALUES ('week', '2020-01-06', 0, 3, 1, 0.25,"
                " $1::uuid) RETURNING retained_cents", BRAND_ID)
            money = await conn.fetchval(
                "SELECT retained_cents FROM cohort_snapshots"
                " WHERE granularity = 'week' AND cohort_period = '2020-01-06'"
                " AND brand_id = $1::uuid", BRAND_ID)
            rejected = False
            try:
                await conn.execute(
                    "INSERT INTO cohort_snapshots (granularity, cohort_period,"
                    " period_offset, cohort_size, retained, retention_rate,"
                    " retained_cents, brand_id)"
                    " VALUES ('week', '2020-01-13', 0, 3, 1, 0.25, -1,"
                    " $1::uuid)", BRAND_ID)
            except Exception:
                rejected = True
    finally:
        await pool.close()

    assert row is not None
    assert int(row["retained_cents"]) == 0
    assert int(money) == 0
    assert rejected, "a negative money was accepted: the check is missing"
