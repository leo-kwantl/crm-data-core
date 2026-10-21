"""Cohort WRITER: the five parts + the disjointness check (data D2, 020/056).

Every check here fails if the writer's change is reverted:

- (1) the writer RE-MEASURES the cells it measures (ON CONFLICT ... DO
      UPDATE on the grain index's expressions), so a cell whose window was
      still open at the first write stops reading 0;
- (2) the money is re-measured, not inherited: a cell written before 052
      read `retained_cents = 0`, which the reader prints as NULL (the ARPU
      was empty for every historical cell);
- (3) ONE vintage per grid: every row the run writes takes the run's write
      time, because the reader reports one `as_of = max(created_at)`;
- (4) the cells the new population no longer produces are DELETED - in
      this granularity and this scope only;
- (5) the row names the population it describes (056's `population_rule`);
- (6) the cohorts are DISJOINT: the grid's per-cohort sizes add up to the
      scope's distinct customers, or the reader's headline overcounts.

The writes go to a throwaway brand/group/store and to member ids outside
the demo seed's space, and every row this file creates is purged in a
`finally`, matched by the predicate that created it.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipelines.snapshot import (
    COHORT_GRID_SIZE_SQL,
    COHORT_SNAPSHOT_UPSERT_SQL,
    POPULATION_RULE,
    assert_cohorts_disjoint,
)

GROUP_CODE = "COHORT-W-GRP"
BRAND_CODE = "COHORT-W-B"
OTHER_BRAND_CODE = "COHORT-W-B2"
STORE_CODE = "COHORT-W-S"
OTHER_STORE_CODE = "COHORT-W-S2"
MEMBERS = ("M-009801", "M-009802")
MIGRATION = (Path(__file__).resolve().parents[3] / "db" / "migrations"
             / "056_cohort_population_rule.sql")


def _ts(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=timezone.utc)


async def _make_scope(conn) -> tuple:
    """A throwaway group + brand + store + two customers."""
    gid = await conn.fetchval(
        "INSERT INTO groups (code, name) VALUES ($1, 'Cohort Writer Group')"
        " ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name"
        " RETURNING id", GROUP_CODE)
    bid = await conn.fetchval(
        "INSERT INTO brands (group_id, code, name)"
        " VALUES ($1, $2, 'Cohort Writer Brand')"
        " ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name"
        " RETURNING id", gid, BRAND_CODE)
    bid2 = await conn.fetchval(
        "INSERT INTO brands (group_id, code, name)"
        " VALUES ($1, $2, 'Cohort Writer Brand 2')"
        " ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name"
        " RETURNING id", gid, OTHER_BRAND_CODE)
    sid = await conn.fetchval(
        "INSERT INTO stores (code, name, brand_id)"
        " VALUES ($1, 'Cohort Writer Store', $2)"
        " ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name"
        " RETURNING id", STORE_CODE, bid)
    sid2 = await conn.fetchval(
        "INSERT INTO stores (code, name, brand_id)"
        " VALUES ($1, 'Cohort Writer Store 2', $2)"
        " ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name"
        " RETURNING id", OTHER_STORE_CODE, bid2)
    ids = {}
    for i, mem in enumerate(MEMBERS, 1):
        ids[mem] = await conn.fetchval(
            "INSERT INTO customers (member_id, full_name) VALUES ($1, $2)"
            " ON CONFLICT (member_id) DO UPDATE SET"
            " full_name = EXCLUDED.full_name RETURNING id",
            mem, f"Cohort Writer C{i}")
    return gid, bid, bid2, sid, sid2, ids


async def _order(conn, customer_id, store_id, when, total=1000) -> None:
    await conn.execute(
        "INSERT INTO orders (customer_id, store_id, ordered_at, total_cents,"
        " status) VALUES ($1, $2, $3, $4, 'completed')",
        customer_id, store_id, when, total)


async def _purge(conn, brand_ids) -> None:
    """Delete exactly what this file created, in dependency order."""
    for bid in brand_ids:
        await conn.execute(
            "DELETE FROM cohort_snapshots WHERE brand_id = $1", bid)
    await conn.execute(
        "DELETE FROM orders WHERE customer_id IN ("
        " SELECT id FROM customers WHERE member_id = ANY($1::text[]))",
        list(MEMBERS))
    await conn.execute(
        "DELETE FROM customers WHERE member_id = ANY($1::text[])",
        list(MEMBERS))
    await conn.execute("DELETE FROM stores WHERE code = ANY($1::text[])",
                       [STORE_CODE, OTHER_STORE_CODE])
    await conn.execute("DELETE FROM brands WHERE code = ANY($1::text[])",
                       [BRAND_CODE, OTHER_BRAND_CODE])
    await conn.execute("DELETE FROM groups WHERE code = $1", GROUP_CODE)


@pytest.mark.asyncio
async def test_writer_re_measures_its_cells_and_their_money(database_url: str):
    """(1)+(2)+(3): a closed window is measured again, and the grid's
    vintage is the run that wrote it.

    c1 first orders in Jan, c2 first orders in Jan. The first run writes
    the grid: Jan/offset-1 has no returning customer yet, so it reads 0
    retained and 0 cents. c1 then orders again in Feb, and the SECOND run
    re-measures that cell: 1 retained, rate 0.5, and the money the
    returning customer brought back.

    Before the change the second run was a no-op (ON CONFLICT DO
    NOTHING): the cell kept reading 0 and 0 cents forever, so the served
    curve read 0 for a cell that had since closed, and the reader printed
    NULL for its ARPU.
    """
    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            gid, bid, _bid2, sid, _sid2, ids = await _make_scope(conn)
            try:
                await _order(conn, ids["M-009801"], sid, _ts(2026, 1, 15))
                await _order(conn, ids["M-009802"], sid, _ts(2026, 1, 20))

                await conn.execute(COHORT_SNAPSHOT_UPSERT_SQL, "month", 2,
                                   bid, None)
                first = await conn.fetchrow(
                    "SELECT cohort_size, retained, retention_rate,"
                    " retained_cents, created_at FROM cohort_snapshots"
                    " WHERE granularity = 'month' AND cohort_period ="
                    " '2026-01-01'::date AND period_offset = 1"
                    " AND brand_id = $1", bid)
                assert first is not None, "the Jan/offset-1 cell is missing"
                assert first["retained"] == 0
                assert first["retained_cents"] == 0
                vintage_1 = first["created_at"]

                # The window closes: c1 comes back in Feb.
                await _order(conn, ids["M-009801"], sid, _ts(2026, 2, 10),
                             total=2500)

                await conn.execute(COHORT_SNAPSHOT_UPSERT_SQL, "month", 2,
                                   bid, None)
                second = await conn.fetchrow(
                    "SELECT cohort_size, retained, retention_rate,"
                    " retained_cents, created_at FROM cohort_snapshots"
                    " WHERE granularity = 'month' AND cohort_period ="
                    " '2026-01-01'::date AND period_offset = 1"
                    " AND brand_id = $1", bid)
                assert second["retained"] == 1, \
                    "the closed window still reads 0 - the writer froze it"
                assert float(second["retention_rate"]) == 0.5
                assert second["retained_cents"] == 2500, \
                    "the money was inherited, not re-measured"
                # (3) the grid's vintage is the run that wrote it.
                assert second["created_at"] > vintage_1, (
                    "the grid kept its first vintage - a read that pairs"
                    " two vintages refuses the pair")
                stamps = await conn.fetch(
                    "SELECT DISTINCT created_at FROM cohort_snapshots"
                    " WHERE granularity = 'month' AND brand_id = $1", bid)
                assert len(stamps) == 1, \
                    "the grid carries more than one vintage"
            finally:
                await _purge(conn, [bid])
                await conn.execute("DELETE FROM groups WHERE code = $1",
                                   GROUP_CODE)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_stale_cells_are_deleted_in_this_granularity_and_scope_only(
        database_url: str):
    """(4): a cell the new population no longer produces is deleted - and
    only this granularity's and this scope's cells are.

    Brand A's January cohort loses its customer (the order is deleted),
    so the second run produces no January cell and the first run's cell
    must be gone. Brand B's cell (another scope) and brand A's WEEK cell
    (another granularity) must survive: the delete is scoped.
    """
    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            gid, bid, bid2, sid, sid2, ids = await _make_scope(conn)
            try:
                await _order(conn, ids["M-009801"], sid, _ts(2026, 1, 15))
                await conn.execute(COHORT_SNAPSHOT_UPSERT_SQL, "month", 1,
                                   bid, None)
                await conn.execute(COHORT_SNAPSHOT_UPSERT_SQL, "week", 1,
                                   bid, None)
                # Another scope, same shape, its OWN store.
                await conn.execute(
                    "INSERT INTO orders (customer_id, store_id, ordered_at,"
                    " total_cents, status)"
                    " SELECT $1, s.id, TIMESTAMPTZ '2026-01-15 12:00+00',"
                    " 1000, 'completed' FROM stores s"
                    " WHERE s.code = $2",
                    ids["M-009802"], OTHER_STORE_CODE)
                await conn.execute(COHORT_SNAPSHOT_UPSERT_SQL, "month", 1,
                                   bid2, None)

                before = await conn.fetchval(
                    "SELECT COUNT(*) FROM cohort_snapshots"
                    " WHERE granularity = 'month' AND brand_id = $1"
                    " AND cohort_period = '2026-01-01'::date", bid)
                assert int(before) == 2, "the January cell is missing"

                # The population shrinks to nothing: the order goes away.
                await conn.execute(
                    "DELETE FROM orders WHERE customer_id = $1",
                    ids["M-009801"])

                await conn.execute(COHORT_SNAPSHOT_UPSERT_SQL, "month", 1,
                                   bid, None)

                stale = await conn.fetchval(
                    "SELECT COUNT(*) FROM cohort_snapshots"
                    " WHERE granularity = 'month' AND brand_id = $1"
                    " AND cohort_period = '2026-01-01'::date", bid)
                assert int(stale) == 0, \
                    "the cell the population no longer produces survived"
                other_scope = await conn.fetchval(
                    "SELECT COUNT(*) FROM cohort_snapshots"
                    " WHERE granularity = 'month' AND brand_id = $1"
                    " AND cohort_period = '2026-01-01'::date", bid2)
                assert int(other_scope) == 2, \
                    "another scope's cells were deleted"
                other_grain = await conn.fetchval(
                    "SELECT COUNT(*) FROM cohort_snapshots"
                    " WHERE granularity = 'week' AND brand_id = $1", bid)
                assert int(other_grain) > 0, \
                    "another granularity's cells were deleted"
            finally:
                await _purge(conn, [bid, bid2])
                await conn.execute("DELETE FROM groups WHERE code = $1",
                                   GROUP_CODE)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_the_cohorts_are_disjoint(database_url: str):
    """(6): the grid's per-cohort sizes add up to the scope's distinct
    customers - the property the reader's headline rests on.

    The reader counts the customers once per cohort (the row repeats the
    cohort's size on every offset row) and sums the cohorts, so an
    overlapping cohort would inflate the headline with no signal. The
    query cannot see it, so the writer asserts it.
    """
    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            gid, bid, _bid2, sid, _sid2, ids = await _make_scope(conn)
            try:
                await _order(conn, ids["M-009801"], sid, _ts(2026, 1, 15))
                await _order(conn, ids["M-009802"], sid, _ts(2026, 1, 20))
                await _order(conn, ids["M-009801"], sid, _ts(2026, 2, 10))

                row = await conn.fetchrow(COHORT_SNAPSHOT_UPSERT_SQL, "month",
                                          2, bid, None)
                population = int(row["population"])
                grid_size_sum = int(await conn.fetchval(
                    COHORT_GRID_SIZE_SQL, "month", bid, None))
                assert population == 2, "the scope's customers are not two"
                assert grid_size_sum == population, \
                    "the grid's per-cohort sizes do not add up to the scope"
                # The assertion itself: an overlapping cohort is refused.
                assert_cohorts_disjoint(2, 2, "month", bid, None)
                with pytest.raises(RuntimeError):
                    assert_cohorts_disjoint(3, 2, "month", bid, None)
            finally:
                await _purge(conn, [bid])
                await conn.execute("DELETE FROM groups WHERE code = $1",
                                   GROUP_CODE)
    finally:
        await pool.close()


def test_the_row_names_the_population_it_describes():
    """(5): the migration's default and the writer's constant are the
    same string, so a row can never name a population we do not measure.
    """
    text = MIGRATION.read_text(encoding="utf-8")
    match = re.search(r"ADD COLUMN population_rule text NOT NULL DEFAULT\s+"
                      r"'([^']*)'", text)
    assert match is not None, "056 does not give population_rule a default"
    assert match.group(1) == POPULATION_RULE
    assert POPULATION_RULE in COHORT_SNAPSHOT_UPSERT_SQL


@pytest.mark.asyncio
async def test_the_writer_writes_the_rule_on_every_row(database_url: str):
    """(5): the row carries the rule, not only the default."""
    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            gid, bid, _bid2, sid, _sid2, ids = await _make_scope(conn)
            try:
                await _order(conn, ids["M-009801"], sid, _ts(2026, 1, 15))
                await conn.execute(COHORT_SNAPSHOT_UPSERT_SQL, "month", 1,
                                   bid, None)
                rules = await conn.fetch(
                    "SELECT DISTINCT population_rule FROM cohort_snapshots"
                    " WHERE brand_id = $1", bid)
                assert [r["population_rule"] for r in rules] == [POPULATION_RULE]
            finally:
                await _purge(conn, [bid])
                await conn.execute("DELETE FROM groups WHERE code = $1",
                                   GROUP_CODE)
    finally:
        await pool.close()
