"""Snapshot-history tests (data owns history).

Pure unit: pct_change / kpi_movement / clv_movement (no DB).
DB: kpi_daily upsert is idempotent per day x scope; 013 tables exist.
"""
from __future__ import annotations

from datetime import date

import pytest

from pipelines.snapshot import (
    KPI_DAILY_UPSERT_SQL,
    KPI_TREND_SQL,
    clv_movement,
    kpi_movement,
    pct_change,
)


def test_pct_change_none_on_zero_base():
    assert pct_change(0, 5) is None
    assert pct_change(100, 150) == 0.5
    assert pct_change(200, 100) == -0.5


def test_kpi_movement_deltas_and_pct():
    prev = {"snapshot_date": "2026-10-07", "customers_total": 100,
            "orders_total": 200, "revenue_cents_total": 10000,
            "orders_day": 10, "revenue_day_cents": 1000,
            "new_customers_day": 2, "active_30d": 50}
    cur = dict(prev, snapshot_date="2026-10-08", orders_day=15,
               revenue_day_cents=1500)
    out = kpi_movement(prev, cur)
    assert out["deltas"]["orders_day"] == {"prev": 10.0, "cur": 15.0,
                                           "delta": 5.0, "pct": 0.5}
    assert out["deltas"]["customers_total"]["pct"] == 0.0


def test_clv_movement_tier_flip_and_threshold():
    prev = {"c1": {"clv_cents": 1000, "value_tier": "mid"},
            "c2": {"clv_cents": 500, "value_tier": "low"},
            "c3": {"clv_cents": 900, "value_tier": "low"}}
    cur = {"c1": {"clv_cents": 1050, "value_tier": "mid"},   # +5%: steady
           "c2": {"clv_cents": 700, "value_tier": "mid"},    # tier flip
           "c4": {"clv_cents": 100, "value_tier": "low"}}    # entered
    out = clv_movement(prev, cur)
    assert out["entered"] == ["c4"]
    assert out["exited"] == ["c3"]
    assert [m["customer_id"] for m in out["moved"]] == ["c2"]


def test_snapshot_sql_present():
    assert "kpi_daily_snapshots" in KPI_DAILY_UPSERT_SQL
    assert "ON CONFLICT DO NOTHING" in KPI_DAILY_UPSERT_SQL
    assert "kpi_daily_snapshots" in KPI_TREND_SQL


@pytest.mark.asyncio
async def test_kpi_daily_upsert_idempotent(database_url: str):
    """Same day x scope twice -> one row (re-run is a no-op)."""
    import re
    from pathlib import Path

    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT to_regclass('public.kpi_daily_snapshots')")
            if exists is None:
                up_re = re.compile(
                    r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)", re.S | re.I)
                mig_dir = (Path(__file__).resolve().parents[3]
                           / "db" / "migrations")
                for mig in sorted(mig_dir.glob("*.sql")):
                    m = up_re.search(mig.read_text(encoding="utf-8"))
                    if m and m.group(1).strip():
                        try:
                            await conn.execute(m.group(1).strip())
                        except Exception as exc:
                            if "already exists" not in str(exc).lower():
                                raise
            as_of = date(2026, 10, 8)
            r1 = await conn.fetchrow(KPI_DAILY_UPSERT_SQL, as_of, None, None)
            r2 = await conn.fetchrow(KPI_DAILY_UPSERT_SQL, as_of, None, None)
            assert r1 is not None
            assert r2 is None  # conflict -> no second row
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM kpi_daily_snapshots"
                " WHERE snapshot_date = $1::date", as_of)
            assert int(n) == 1
    finally:
        await pool.close()
