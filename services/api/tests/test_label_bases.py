"""The base of every reader-facing integer label, in the DDL itself.

A label whose base is only discoverable in another repo's code is the bug
waiting to happen: app bound `segment - 1` into a members query and served
each segment's previous segment's customers, because the column's CHECK
(`segment >= 0`) permitted the 0 it assumed.

This file holds three rules:

- the segment label is 1-based, and the DDL refuses 0;
- the cluster index is 0-based, and the DDL says so - the two families have
  different bases ON PURPOSE, so mixing them is a visible mistake;
- every reader-facing integer label states its base in a COMMENT, in the
  file the next reader reads first.

Rows written here live in this module's own keyspace and are purged at
module teardown (the suite shares one database across files and is not
transactional).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Iterator

import pytest

MEMBER_ID = "M-901001"
RUN_KEYS = "label-base"

# (table, column, the base the comment must state)
LABELLED_COLUMNS = [
    ("segment_assignments", "segment", "1-based"),
    ("rec_items", "rank", "1-based"),
    ("membership_tiers", "rank", "1-based"),
    ("rfm_customer_scores", "r", "1-based"),
    ("rfm_customer_scores", "f", "1-based"),
    ("rfm_customer_scores", "m", "1-based"),
    ("explain_rows", "cluster", "0-based"),
    ("cluster_feature_matrix", "cluster", "0-based"),
    ("cluster_feature_matrix_long", "cluster", "0-based"),
]


@pytest.fixture(scope="module", autouse=True)
def _no_residue(database_url: str) -> Iterator[None]:
    """Delete the rows this module created, in a finally."""
    import asyncio

    async def _purge() -> None:
        import asyncpg

        conn = await asyncpg.connect(database_url)
        try:
            await conn.execute(
                "DELETE FROM segment_assignments WHERE run_id IN"
                " (SELECT id FROM segment_runs WHERE seed = $1)", -901)
            await conn.execute(
                "DELETE FROM segment_runs WHERE seed = $1", -901)
            await conn.execute(
                "DELETE FROM customers WHERE member_id = $1", MEMBER_ID)
        finally:
            await conn.close()

    yield
    asyncio.run(_purge())


@pytest.mark.asyncio
async def test_the_segment_label_is_one_based_and_refuses_zero(
        database_url: str) -> None:
    """`segment = 0` is refused: the label is 1..k, and the DDL says so."""
    import asyncpg

    conn = await asyncpg.connect(database_url)
    try:
        customer_id = await conn.fetchval(
            "INSERT INTO customers (member_id, full_name)"
            " VALUES ($1, 'Label Base Tester')"
            " ON CONFLICT (member_id) DO UPDATE SET full_name = EXCLUDED.full_name"
            " RETURNING id", MEMBER_ID)
        run_id = await conn.fetchval(
            "INSERT INTO segment_runs (as_of, mode, k, seed, customer_count)"
            " VALUES ($1, 'purchase', 5, -901, 1) RETURNING id", date(2026, 1, 1))
        await conn.execute(
            "INSERT INTO segment_assignments (run_id, customer_id, segment, label)"
            " VALUES ($1, $2, 1, 'Tester')", run_id, customer_id)

        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO segment_assignments"
                " (run_id, customer_id, segment, label)"
                " VALUES ($1, $2, 0, 'Tester Zero')", run_id, customer_id)

        stored = await conn.fetchval(
            "SELECT count(*) FROM segment_assignments WHERE run_id = $1",
            run_id)
        assert int(stored) == 1, "the refused row was stored anyway"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_every_reader_facing_label_states_its_base(
        database_url: str) -> None:
    """The base is in the DDL's own text, not in another repo's code."""
    import asyncpg

    conn = await asyncpg.connect(database_url)
    try:
        for table, column, base in LABELLED_COLUMNS:
            comment = await conn.fetchval(
                "SELECT col_description(c.oid, a.attnum) FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " JOIN pg_attribute a ON a.attrelid = c.oid"
                " WHERE c.relname = $1 AND n.nspname = 'public'"
                "   AND a.attname = $2", table, column)
            assert comment, f"{table}.{column} states no base"
            assert base in comment, (
                f"{table}.{column} does not say it is {base}: {comment!r}")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_two_bases_differ_on_purpose(database_url: str) -> None:
    """Segment label 1-based, cluster index 0-based - the DDL encodes both."""
    import asyncpg

    conn = await asyncpg.connect(database_url)
    try:
        checks = {}
        for table, column in (("segment_assignments", "segment"),
                              ("rec_items", "rank"),
                              ("membership_tiers", "rank"),
                              ("explain_rows", "cluster"),
                              ("cluster_feature_matrix", "cluster"),
                              ("cluster_feature_matrix_long", "cluster")):
            rows = await conn.fetch(
                "SELECT pg_get_constraintdef(c.oid) AS def FROM pg_constraint c"
                " WHERE c.contype = 'c' AND c.conrelid = $1::regclass"
                "   AND pg_get_constraintdef(c.oid) LIKE '%' || $2 || '%'",
                table, column)
            checks[table] = [r["def"] for r in rows]
        assert checks["segment_assignments"], "no check on segment_assignments"
        assert any("segment >= 1" in d for d in checks["segment_assignments"]), \
            checks["segment_assignments"]
        assert any("rank >= 1" in d for d in checks["rec_items"]), \
            checks["rec_items"]
        for table in ("explain_rows", "cluster_feature_matrix",
                      "cluster_feature_matrix_long"):
            assert any("cluster >= 0" in d for d in checks[table]), (
                f"{table} is the 0-based family and must keep permitting 0: "
                f"{checks[table]}")
    finally:
        await conn.close()
