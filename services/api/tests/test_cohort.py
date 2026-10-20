"""Cohort-retention snapshot tests (data D2, 020).

Pure unit: retention_rate edges + SQL shape. DB: month-grain upsert over
seeded orders produces the agreed grain rows; re-run is a no-op.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipelines.snapshot import (
    BUSINESS_TZ_NAME,
    BUSINESS_TZ_OFFSET_HOURS,
    BUSINESS_TZ_SQL,
    COHORT_GRANULARITIES,
    COHORT_SNAPSHOT_UPSERT_SQL,
    COHORT_TREND_SQL,
    retention_rate,
)

DEMO_BRAND = "60100000-0000-0000-0000-000000000001"


def test_retention_rate_edges():
    assert retention_rate(0, 0) is None
    assert retention_rate(2, 2) == 1.0
    assert retention_rate(2, 1) == 0.5
    assert retention_rate(4, 0) == 0.0


def test_cohort_sql_shape():
    assert set(COHORT_GRANULARITIES) == {"week", "month", "year"}
    assert "cohort_snapshots" in COHORT_SNAPSHOT_UPSERT_SQL
    assert "ON CONFLICT DO NOTHING" in COHORT_SNAPSHOT_UPSERT_SQL
    assert "cohort_snapshots" in COHORT_TREND_SQL
    # Business-day rule (contracts.md): every trunc that decides a bucket
    # carries the +08 offset, and the zone NAME is carried with it so the
    # offset never floats free.
    assert BUSINESS_TZ_NAME == "Asia/Hong_Kong"
    assert BUSINESS_TZ_OFFSET_HOURS == 8
    assert BUSINESS_TZ_SQL == "AT TIME ZONE interval '8 hours'"
    assert COHORT_SNAPSHOT_UPSERT_SQL.count(BUSINESS_TZ_SQL) == 2
    # Bare trunc = the defect this guards (buckets were UTC on a GMT server).
    assert "date_trunc($1::text, MIN(ordered_at))" \
        not in COHORT_SNAPSHOT_UPSERT_SQL


COHORT_MEMBERS = ("M-000021", "M-000022", "M-000023")


@pytest.mark.asyncio
async def test_cohort_month_upsert_and_rerun_noop(database_url: str):
    """Jan cohort {c1, c2}, Feb cohort {c3}; c1 re-orders in Feb."""
    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            store = await conn.fetchval(
                "INSERT INTO stores (code, name, brand_id)"
                " VALUES ('COHORT-T', 'Cohort Test Store', $1::uuid)"
                " ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name"
                " RETURNING id", DEMO_BRAND)
            ids = {}
            for i, mem in enumerate(("M-000021", "M-000022", "M-000023"), 1):
                cid = await conn.fetchval(
                    "INSERT INTO customers (member_id, full_name)"
                    " VALUES ($1, $2)"
                    " ON CONFLICT (member_id) DO UPDATE"
                    " SET full_name = EXCLUDED.full_name RETURNING id",
                    mem, f"Cohort C{i}")
                ids[mem] = cid

            async def order(mem, when, total=1000):
                await conn.execute(
                    "INSERT INTO orders (customer_id, store_id, ordered_at,"
                    " total_cents, status) VALUES ($1, $2, $3, $4,"
                    " 'completed')",
                    ids[mem], store, when, total)

            def ts(y, m, d):
                return datetime(y, m, d, 12, 0, tzinfo=timezone.utc)

            await order("M-000021", ts(2026, 1, 15))  # c1 first Jan
            await order("M-000022", ts(2026, 1, 20))  # c2 Jan only
            await order("M-000021", ts(2026, 2, 10))  # c1 retained Feb
            await order("M-000023", ts(2026, 2, 5))   # c3 first Feb

            await conn.execute(COHORT_SNAPSHOT_UPSERT_SQL, "month", 2,
                               None, None)
            rows = await conn.fetch(
                "SELECT cohort_period::text AS p, period_offset AS k,"
                " cohort_size AS n, retained AS r, retention_rate AS rate"
                " FROM cohort_snapshots WHERE granularity = 'month'"
                " AND brand_id IS NULL AND group_id IS NULL"
                " AND cohort_period >= '2026-01-01'::date"
                " ORDER BY p, k")
            got = {(r["p"], r["k"]): (r["n"], r["r"], r["rate"])
                   for r in rows}
            assert got[("2026-01-01", 0)] == (2, 2, 1.0)
            assert got[("2026-01-01", 1)] == (2, 1, 0.5)
            assert got[("2026-01-01", 2)] == (2, 0, 0.0)
            assert got[("2026-02-01", 0)] == (1, 1, 1.0)
            assert got[("2026-02-01", 1)] == (1, 0, 0.0)

            n_before = await conn.fetchval(
                "SELECT COUNT(*) FROM cohort_snapshots"
                " WHERE granularity = 'month'")
            await conn.execute(COHORT_SNAPSHOT_UPSERT_SQL, "month", 2,
                               None, None)
            n_after = await conn.fetchval(
                "SELECT COUNT(*) FROM cohort_snapshots"
                " WHERE granularity = 'month'")
            assert int(n_before) == int(n_after)

            trend = await conn.fetch(COHORT_TREND_SQL, "month", None, None)
            assert any(r["cohort_period"].isoformat() == "2026-01-01"
                       for r in trend)
    finally:
        # Purge what this file created, matched by the predicates that
        # created it: the orders, the three customers and the store. A
        # leaked store survives every run and shows up on /stores as a
        # product row nobody owns.
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM order_items WHERE order_id IN ("
                " SELECT o.id FROM orders o JOIN customers c"
                " ON c.id = o.customer_id"
                " WHERE c.member_id = ANY($1::text[]))",
                COHORT_MEMBERS)
            await conn.execute(
                "DELETE FROM orders WHERE customer_id IN ("
                " SELECT id FROM customers"
                " WHERE member_id = ANY($1::text[]))",
                COHORT_MEMBERS)
            await conn.execute(
                "DELETE FROM customers WHERE member_id = ANY($1::text[])",
                COHORT_MEMBERS)
            await conn.execute("DELETE FROM stores WHERE code = 'COHORT-T'")
        await pool.close()


@pytest.mark.asyncio
async def test_cohort_bucket_instant_is_business_tz(database_url: str):
    """The cohort job buckets an order by its +08 instant, not the session zone.

    2025-09-30T18:00Z is 2025-10-01T02:00+08, so the business month is
    2025-10-01; a UTC-truncating implementation files it under 2025-09-01.
    This runs the SHIPPED COHORT_SNAPSHOT_UPSERT_SQL (not a copy of its
    expression) against a throwaway group/brand/store/customer created
    inside a transaction that is always rolled back — so the assertion is
    on the real artifact and leaves no rows and no dummy customers behind.

    Host note: on a +08 server a reverted bare trunc would coincide with
    the right answer, which is why the zone's presence in the SQL is
    asserted separately (test_cohort_sql_shape) — the host-independent
    half of this guard.
    """
    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            gid = await conn.fetchval(
                "INSERT INTO groups (code, name)"
                " VALUES ('COHORT-TZ-T', 'Cohort TZ Test') RETURNING id")
            bid = await conn.fetchval(
                "INSERT INTO brands (group_id, code, name)"
                " VALUES ($1, 'COHORT-TZ-T', 'Cohort TZ Test')"
                " RETURNING id", gid)
            sid = await conn.fetchval(
                "INSERT INTO stores (code, name, brand_id)"
                " VALUES ('COHORT-TZ-T', 'Cohort TZ Test Store', $1)"
                " RETURNING id", bid)
            cid = await conn.fetchval(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ('M-009901', 'Cohort TZ C1') RETURNING id")
            await conn.execute(
                "INSERT INTO orders (customer_id, store_id, ordered_at,"
                " total_cents, status) VALUES ($1, $2,"
                " TIMESTAMPTZ '2025-09-30 18:00:00+00', 1000, 'completed')",
                cid, sid)

            await conn.execute(COHORT_SNAPSHOT_UPSERT_SQL, "month", 0,
                               bid, None)
            oct_size = await conn.fetchval(
                "SELECT cohort_size FROM cohort_snapshots"
                " WHERE granularity = 'month'"
                " AND cohort_period = '2025-10-01'::date"
                " AND period_offset = 0 AND brand_id = $1", bid)
            sep_size = await conn.fetchval(
                "SELECT cohort_size FROM cohort_snapshots"
                " WHERE granularity = 'month'"
                " AND cohort_period = '2025-09-01'::date"
                " AND period_offset = 0 AND brand_id = $1", bid)
            assert oct_size == 1, "order not bucketed in the +08 month"
            assert sep_size is None, "order bucketed in the UTC month"
            await tx.rollback()
    finally:
        await pool.close()
