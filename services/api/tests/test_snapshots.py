"""Snapshot-history tests (data owns history).

Pure unit: pct_change / kpi_movement / clv_movement (no DB).
DB: kpi_daily upsert is idempotent per day x scope; 013 tables exist.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from pipelines.snapshot import (
    KPI_DAILY_UPSERT_SQL,
    KPI_TREND_SQL,
    LATEST_COHORT_DATE_SQL,
    LATEST_KPI_DATE_SQL,
    BUSINESS_TZ_NAME,
    business_today,
    clv_movement,
    kpi_movement,
    pct_change,
    resolve_asof,
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
async def test_kpi_day_bounds_are_business_tz_not_session_zone(database_url: str):
    """The day bucket is +08 even when the connection's zone is GMT.

    2027-03-01T20:00:00Z is 2027-03-02 04:00 in HK, so the 03-02 KPI day
    owns it. Bucketed with the session zone (this cluster runs GMT) the
    order falls on 03-01 and orders_day for 03-02 reads 0 -- the review's
    §4.3-1 (published day numbers disagree with every +08 surface).
    """
    import asyncpg

    assert "AT TIME ZONE interval '8 hours'" in KPI_DAILY_UPSERT_SQL, (
        "the KPI day bounds must pin the +08 business day explicitly;"
        " a bare ::date/date_trunc truncates in the session zone")
    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await conn.execute("SET timezone = 'GMT'")
            demo_brand = "60100000-0000-0000-0000-000000000001"
            sid = await conn.fetchval(
                "INSERT INTO stores (code, name, brand_id)"
                " VALUES ('KPI-TZ-T', 'KPI TZ Store', $1)"
                " ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name"
                " RETURNING id", demo_brand)
            cid = await conn.fetchval(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ('M-009902', 'KPI TZ C1')"
                " ON CONFLICT (member_id) DO UPDATE"
                " SET full_name = EXCLUDED.full_name RETURNING id")
            await conn.execute(
                "DELETE FROM orders WHERE customer_id = $1", cid)
            await conn.execute(
                "INSERT INTO orders (customer_id, store_id, ordered_at,"
                " total_cents, status) VALUES ($1, $2,"
                " TIMESTAMPTZ '2027-03-01 20:00:00+00', 4321, 'completed')",
                cid, sid)
            as_of = date(2027, 3, 2)
            await conn.execute(
                "DELETE FROM kpi_daily_snapshots WHERE snapshot_date = $1"
                " AND brand_id IS NULL AND group_id IS NULL", as_of)
            await conn.fetchrow(KPI_DAILY_UPSERT_SQL, as_of, None, None)
            row = await conn.fetchrow(
                "SELECT orders_day, revenue_day_cents FROM"
                " kpi_daily_snapshots WHERE snapshot_date = $1"
                " AND brand_id IS NULL AND group_id IS NULL", as_of)
            assert row is not None
            assert int(row["orders_day"]) == 1, (
                "a +08 evening order must land on the next business day")
            assert int(row["revenue_day_cents"]) == 4321
    finally:
        await pool.close()


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


def test_resolve_asof_latest_returns_data_max():
    assert (resolve_asof("latest", date(2026, 10, 5), date(2026, 10, 9))
            == date(2026, 10, 5))


def test_resolve_asof_current_returns_yesterday():
    assert (resolve_asof("current", date(2026, 10, 5), date(2026, 10, 9))
            == date(2026, 10, 8))


def test_business_today_is_business_calendar_not_host_clock():
    """+08 business day, computed from the wall clock, not the host.

    Guards the class of defect the cohort/`current` fix removed: an
    implicit environmental clock (host `date.today()` or server
    CURRENT_DATE) deciding a business boundary. Server here runs GMT and
    the host is +08, so `date.today()` is right for 16 h a day and wrong
    for the other 8 — the failure is invisible in daylight.
    """
    assert business_today() == (
        datetime.now(timezone(timedelta(hours=8))).date())
    assert BUSINESS_TZ_NAME == "Asia/Hong_Kong"


def test_resolve_asof_rejects_unknown_mode():
    with pytest.raises(ValueError):
        resolve_asof("whenever", date(2026, 10, 5), date(2026, 10, 9))


def test_latest_cutoff_sql_present():
    assert "MAX(snapshot_date)" in LATEST_KPI_DATE_SQL
    assert "MAX(cohort_period)" in LATEST_COHORT_DATE_SQL


def test_ld11_validate_report_definition_ok():
    from pipelines.reports import validate_report_definition
    out = validate_report_definition({
        "report_id": "rfm", "name": "RFM Segments", "kind": "segments",
        "grain": "customer",
        "definition": {"axes": ["recency", "frequency"],
                       "metrics": ["count", "revenue_cents"],
                       "rfm_note": "quintiles per scope"}})
    assert out["version"] == 1
    assert out["definition"]["rfm_note"].startswith("quintiles")


def test_ld11_validate_rejects_thin_specs():
    from pipelines.reports import validate_report_definition
    with pytest.raises(ValueError, match="missing"):
        validate_report_definition({"report_id": "x"})
    with pytest.raises(ValueError, match="at least one"):
        validate_report_definition({
            "report_id": "x", "name": "X", "kind": "k", "grain": "g",
            "definition": {"rfm_note": "no renderable content"}})
    with pytest.raises(ValueError, match="version"):
        validate_report_definition({
            "report_id": "x", "name": "X", "kind": "k", "grain": "g",
            "version": 0,
            "definition": {"metrics": ["count"]}})


@pytest.mark.asyncio
async def test_ld11_report_versioning_latest_wins(database_url: str):
    """New version = new row; readers take MAX(version); re-insert of
    the same version is a no-op (immutable history)."""
    import json
    import re
    from pathlib import Path

    import asyncpg

    from pipelines.reports import (
        LATEST_DEFINITIONS_SQL,
        REPORT_UPSERT_SQL,
    )

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT to_regclass('public.report_definitions')")
            if exists is None:
                up_re = re.compile(
                    r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)",
                    re.S | re.I)
                mig = (Path(__file__).resolve().parents[3]
                       / "db" / "migrations"
                       / "026_report_definitions.sql")
                m = up_re.search(mig.read_text(encoding="utf-8"))
                assert m is not None
                await conn.execute(m.group(1).strip())
            for ver, metrics in ((1, ["count"]), (2, ["count", "ltv"])):
                await conn.execute(
                    REPORT_UPSERT_SQL, "ld11-probe", ver, "Probe",
                    "custom", "customer",
                    json.dumps({"metrics": metrics}))
            again = await conn.execute(
                REPORT_UPSERT_SQL, "ld11-probe", 2, "Probe",
                "custom", "customer", json.dumps({"metrics": ["count"]}))
            assert again == "INSERT 0 0"  # conflict -> no-op
            rows = await conn.fetch(LATEST_DEFINITIONS_SQL)
            probe = [r for r in rows if r["report_id"] == "ld11-probe"]
            assert len(probe) == 1 and probe[0]["version"] == 2
            got = json.loads(probe[0]["definition"])
            assert got == {"metrics": ["count", "ltv"]}
            await conn.execute(
                "DELETE FROM report_definitions WHERE report_id = $1",
                "ld11-probe")
    finally:
        await pool.close()
