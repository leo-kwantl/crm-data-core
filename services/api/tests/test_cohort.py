"""Cohort-retention snapshot tests (data D2, 020).

Pure unit: retention_rate edges + SQL shape. DB: month-grain upsert over
seeded orders produces the agreed grain rows; re-run is a no-op.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipelines.snapshot import (
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
        await pool.close()
