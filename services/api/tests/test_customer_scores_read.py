"""The customer list's value and risk: one snapshot, one row per customer.

The list has to answer "who is my most valuable customer" and "who is about
to leave" out of the scores the nightly writes. Two rules make that answer
honest, and this file holds both:

- a VINTAGE with every number - which snapshot, which as_of, which
  (activity, horizon) pair - so a reader can see when it was produced;
- a SCOPE - a brand session never reads another brand's score, and a customer
  the snapshot does not carry reads NULL, never $0 and never a band.

The scope pair is the SNAPSHOT'S STAMP, in app's convention
(`services/api/analytics/scope.py` `session_scope`): brand-scoped staff read
`(brand_id, NULL)`, a group-wide role reads `(NULL, group_id)`, an HQ session
reads `(NULL, NULL)`. Visibility is stamp-equality on both axes, so a brand
session sees its own brand's snapshot and no group's and no global one.

The join must also not multiply the page: the tables hold 19-20 snapshots per
customer, so the read picks ONE snapshot (the newest in scope) and looks each
customer up through the (snapshot_id, customer_id) primary key.

This file writes into its OWN brands and deletes every row it created in a
`finally`, so a shared-database run leaves the row counts where it found them.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from typing import Iterator

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

DATA_ROOT = pathlib.Path(__file__).resolve().parents[3]
QUERIES = DATA_ROOT / "db" / "queries" / "customers.sql"

GROUP_ID = "c0c00000-0000-0000-0000-0000000000d1"
BRAND_A = "b0b00000-0000-0000-0000-0000000000d1"
SCORED_MEMBER = "M-900501"
UNSCORED_MEMBER = "M-900502"

CLV_HORIZON = 90
CHURN_ACTIVITY = "orders"
CHURN_HORIZON = 90


def _statement(name: str) -> str:
    """The named statement out of db/queries/customers.sql (its -- name: markers)."""
    text = QUERIES.read_text(encoding="utf-8")
    chunks = text.split("-- name: ")
    for chunk in chunks[1:]:
        head, _, body = chunk.partition("\n")
        if head.strip() == name:
            return "\n".join(
                ln for ln in body.splitlines()
                if not ln.strip().startswith(("-- name:", "-- params:")))
    raise AssertionError(f"{name} is not in {QUERIES}")


async def _purge(dsn: str) -> None:
    """Delete this file's rows: the scores and snapshots first (the brand
    rows are held by the snapshots' foreign keys), then the customers."""
    import asyncpg

    pool = await asyncpg.create_pool(dsn)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await _wipe(conn)
    finally:
        await pool.close()


async def _wipe(conn) -> None:  # type: ignore[no-untyped-def]
    """This file's rows, and nothing else: the score rows, then the snapshots
    they hang on, then the customers, the brand and the group."""
    await conn.execute(
        "DELETE FROM clv_customer_scores WHERE snapshot_id IN"
        " (SELECT id FROM clv_snapshots WHERE brand_id = ANY($1::uuid[]))",
        [BRAND_A])
    await conn.execute(
        "DELETE FROM churn_customer_scores WHERE snapshot_id IN"
        " (SELECT id FROM churn_snapshots WHERE brand_id = ANY($1::uuid[]))",
        [BRAND_A])
    await conn.execute(
        "DELETE FROM clv_snapshots WHERE brand_id = ANY($1::uuid[])", [BRAND_A])
    await conn.execute(
        "DELETE FROM churn_snapshots WHERE brand_id = ANY($1::uuid[])", [BRAND_A])
    await conn.execute(
        "DELETE FROM clv_snapshots WHERE group_id = $1::uuid", GROUP_ID)
    await conn.execute(
        "DELETE FROM churn_snapshots WHERE group_id = $1::uuid", GROUP_ID)
    await conn.execute(
        "DELETE FROM customers WHERE member_id = ANY($1::text[])",
        [SCORED_MEMBER, UNSCORED_MEMBER])
    await conn.execute("DELETE FROM brands WHERE id = ANY($1::uuid[])", [BRAND_A])
    await conn.execute("DELETE FROM groups WHERE id = $1::uuid", GROUP_ID)


@pytest_asyncio.fixture()
async def db_pool(database_url: str):
    """Function-loop pool; migrates idempotently (no cross-loop session-pool dep)."""
    import asyncpg

    own = await asyncpg.create_pool(database_url)
    async with own.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('public.customers')")
        if exists is None:
            up_re = re.compile(
                r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)", re.S | re.I)
            mig_dir = DATA_ROOT / "db" / "migrations"
            for mig in sorted(mig_dir.glob("*.sql")):
                m = up_re.search(mig.read_text(encoding="utf-8"))
                if m and m.group(1).strip():
                    try:
                        await conn.execute(m.group(1).strip())
                    except Exception as exc:
                        if "already exists" not in str(exc).lower():
                            raise
    yield own
    await own.close()


@pytest.fixture(scope="module", autouse=True)
def _no_residue(database_url: str) -> Iterator[None]:
    yield
    asyncio.run(_purge(database_url))


async def _seed(conn) -> dict:  # type: ignore[no-untyped-def]
    """One brand, two customers, and the snapshots a reader must choose between.

    The snapshots carry the three stamps the sessions carry: a brand stamp
    (brand_id, NULL), a group stamp (NULL, group_id) and a global stamp
    (NULL, NULL). The brand also carries an older snapshot (so "the newest" is
    testable) and a future-dated one (so the vintage bound is testable).
    """
    today = await conn.fetchval(
        "SELECT (now() AT TIME ZONE interval '8 hours')::date")
    yesterday = await conn.fetchval(
        "SELECT (now() AT TIME ZONE interval '8 hours')::date - 1")
    tomorrow = await conn.fetchval(
        "SELECT (now() AT TIME ZONE interval '8 hours')::date + 1")

    await _wipe(conn)
    await conn.execute(
        "INSERT INTO groups (id, code, name) VALUES ($1::uuid, 'SCORES-GRP',"
        " 'Customer Scores Group') ON CONFLICT (id) DO NOTHING", GROUP_ID)
    await conn.execute(
        "INSERT INTO brands (id, group_id, code, name)"
        " VALUES ($1::uuid, $2::uuid, 'SCORES-BRND-A', 'Customer Scores Brand A')"
        " ON CONFLICT (id) DO NOTHING", BRAND_A, GROUP_ID)
    for member_id, name in ((SCORED_MEMBER, "Scores Buyer"),
                            (UNSCORED_MEMBER, "Scores Unscored")):
        await conn.execute(
            "INSERT INTO customers (member_id, full_name, group_id)"
            " VALUES ($1, $2, $3::uuid) ON CONFLICT (member_id) DO NOTHING",
            member_id, name, GROUP_ID)
    scored = await conn.fetchval(
        "SELECT id FROM customers WHERE member_id = $1", SCORED_MEMBER)
    unscored = await conn.fetchval(
        "SELECT id FROM customers WHERE member_id = $1", UNSCORED_MEMBER)

    # The newest CLV snapshot of the brand, and an older one that must never
    # be read while the newer one exists.
    newest = await conn.fetchval(
        "INSERT INTO clv_snapshots (as_of, horizon_days, brand_id, customer_count,"
        " created_by) VALUES ($1::date, $2::int, $3::uuid, 2, 'test')"
        " RETURNING id", today, CLV_HORIZON, BRAND_A)
    older = await conn.fetchval(
        "INSERT INTO clv_snapshots (as_of, horizon_days, brand_id, customer_count,"
        " created_by) VALUES ($1::date, $2::int, $3::uuid, 2, 'test')"
        " RETURNING id", yesterday, CLV_HORIZON, BRAND_A)
    await conn.execute(
        "INSERT INTO clv_customer_scores (snapshot_id, customer_id, p_alive,"
        " expected_purchases, avg_order_value_cents, clv_cents, value_tier)"
        " VALUES ($1::uuid, $2::uuid, 1.0, 3.0, 500.0, 1234.5, 'high')",
        newest, scored)
    await conn.execute(
        "INSERT INTO clv_customer_scores (snapshot_id, customer_id, p_alive,"
        " expected_purchases, avg_order_value_cents, clv_cents, value_tier)"
        " VALUES ($1::uuid, $2::uuid, 1.0, 3.0, 500.0, 999999.0, 'low')",
        older, scored)

    # The group stamp: read by the session whose scope is exactly that stamp,
    # and by nobody else. The GLOBAL stamp is not seeded here on purpose: a
    # (NULL, NULL) snapshot is unique per day across the whole database, so
    # seeding one would collide with a real global snapshot on a shared
    # cluster - and purging it would delete the real one. The global case is
    # therefore tested as isolation below, not as a seeded row.
    group_snap = await conn.fetchval(
        "INSERT INTO clv_snapshots (as_of, horizon_days, group_id, customer_count,"
        " created_by) VALUES ($1::date, $2::int, $3::uuid, 2, 'test')"
        " RETURNING id", today, CLV_HORIZON, GROUP_ID)
    await conn.execute(
        "INSERT INTO clv_customer_scores (snapshot_id, customer_id, p_alive,"
        " expected_purchases, avg_order_value_cents, clv_cents, value_tier)"
        " VALUES ($1::uuid, $2::uuid, 1.0, 3.0, 500.0, 222.0, 'high')",
        group_snap, scored)

    # The churn snapshot of the brand, plus a future-dated one: a snapshot that
    # is dated after today is not a fact this system has yet.
    churn = await conn.fetchval(
        "INSERT INTO churn_snapshots (as_of, activity, horizon_days, brand_id,"
        " customer_count, created_by)"
        " VALUES ($1::date, $2::text, $3::int, $4::uuid, 2, 'test')"
        " RETURNING id", today, CHURN_ACTIVITY, CHURN_HORIZON, BRAND_A)
    await conn.execute(
        "INSERT INTO churn_customer_scores (snapshot_id, customer_id, recency_days,"
        " p_alive, expected_purchases, band)"
        " VALUES ($1::uuid, $2::uuid, 12.5, 0.25, 1.5, 'at-risk')", churn, scored)
    future = await conn.fetchval(
        "INSERT INTO churn_snapshots (as_of, activity, horizon_days, brand_id,"
        " customer_count, created_by)"
        " VALUES ($1::date, $2::text, $3::int, $4::uuid, 2, 'test')"
        " RETURNING id", tomorrow, CHURN_ACTIVITY, CHURN_HORIZON, BRAND_A)
    await conn.execute(
        "INSERT INTO churn_customer_scores (snapshot_id, customer_id, recency_days,"
        " p_alive, expected_purchases, band)"
        " VALUES ($1::uuid, $2::uuid, 0.0, 0.99, 9.0, 'active')", future, scored)

    return {
        "scored": scored, "unscored": unscored, "newest": newest, "older": older,
        "churn": churn, "future": future, "group_snap": group_snap,
        "today": today,
    }


@pytest.mark.asyncio
async def test_the_newest_snapshot_wins_and_the_page_does_not_multiply(
        db_pool) -> None:
    """One snapshot per scope, one row per customer, whatever the history."""
    clv_sql = _statement("page_clv_scores")
    async with db_pool.acquire() as conn:
        ids = await _seed(conn)
        rows = await conn.fetch(
            clv_sql, [ids["scored"], ids["unscored"]], BRAND_A, None,
            CLV_HORIZON)

    # Two customers in, two rows out: the 19-20 snapshots per customer never
    # multiply the page.
    assert len(rows) == 2, rows
    by_customer = {r["customer_id"]: r for r in rows}
    scored = by_customer[ids["scored"]]
    unscored = by_customer[ids["unscored"]]

    # The newest snapshot is the one read - the older snapshot's 999999 is not
    # anywhere in the payload.
    assert scored["clv_snapshot_id"] == ids["newest"], scored
    assert float(scored["clv_cents"]) == 1234.5, scored
    assert scored["value_tier"] == "high", scored
    assert scored["clv_as_of"] == ids["today"], scored
    assert int(scored["clv_horizon_days"]) == CLV_HORIZON, scored
    assert ids["older"] not in {r["clv_snapshot_id"] for r in rows}

    # A customer the snapshot does not carry reads NULL - not $0, not a tier -
    # and the vintage still travels, so a reader can tell "no snapshot for this
    # scope" from "not scored in this snapshot".
    assert unscored["clv_cents"] is None, unscored
    assert unscored["value_tier"] is None, unscored
    assert unscored["clv_as_of"] == ids["today"], unscored
    assert unscored["clv_snapshot_id"] == ids["newest"], unscored


@pytest.mark.asyncio
async def test_the_scope_of_the_score_is_the_scope_of_the_list(
        db_pool) -> None:
    """Stamp-equality: a session reads the snapshot stamped for its scope."""
    clv_sql = _statement("page_clv_scores")
    async with db_pool.acquire() as conn:
        ids = await _seed(conn)

        # A brand session: (brand_id, NULL) reads the brand's own snapshot.
        brand_rows = await conn.fetch(
            clv_sql, [ids["scored"]], BRAND_A, None, CLV_HORIZON)
        # A group-wide role: (NULL, group_id) reads the group's snapshot.
        group_rows = await conn.fetch(
            clv_sql, [ids["scored"]], None, GROUP_ID, CLV_HORIZON)
        # An HQ session: (NULL, NULL) reads no brand's and no group's snapshot.
        hq_rows = await conn.fetch(
            clv_sql, [ids["scored"]], None, None, CLV_HORIZON)

    assert len(brand_rows) == 1, brand_rows
    assert brand_rows[0]["clv_snapshot_id"] == ids["newest"], brand_rows
    assert float(brand_rows[0]["clv_cents"]) == 1234.5, brand_rows

    assert len(group_rows) == 1, group_rows
    assert group_rows[0]["clv_snapshot_id"] == ids["group_snap"], group_rows
    assert float(group_rows[0]["clv_cents"]) == 222.0, group_rows

    # The equality runs both ways: the HQ session reads neither the brand's
    # snapshot nor the group's. (Whether it reads a global snapshot is the
    # database's business, not this file's - a global snapshot is unique per
    # day across every scope, so it cannot be seeded here.)
    assert len(hq_rows) == 1, hq_rows
    assert hq_rows[0]["clv_snapshot_id"] not in (
        ids["newest"], ids["group_snap"]), hq_rows

    # A scope with no snapshot at all reads nothing - and says so, with a NULL
    # vintage rather than a number without a date.
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            clv_sql, [ids["scored"]],
            "00000000-0000-0000-0000-0000000000ff", None, CLV_HORIZON)
    assert len(rows) == 1, rows
    assert rows[0]["clv_snapshot_id"] is None, rows
    assert rows[0]["clv_as_of"] is None, rows
    assert rows[0]["clv_cents"] is None, rows


@pytest.mark.asyncio
async def test_the_churn_band_travels_with_its_pair_and_its_date(
        db_pool) -> None:
    """A band without its (activity, horizon) and its date is not a risk."""
    churn_sql = _statement("page_churn_scores")
    async with db_pool.acquire() as conn:
        ids = await _seed(conn)
        rows = await conn.fetch(
            churn_sql, [ids["scored"], ids["unscored"]], BRAND_A, None,
            CHURN_ACTIVITY, CHURN_HORIZON)

    assert len(rows) == 2, rows
    by_customer = {r["customer_id"]: r for r in rows}
    scored = by_customer[ids["scored"]]
    unscored = by_customer[ids["unscored"]]

    assert scored["band"] == "at-risk", scored
    assert float(scored["p_alive"]) == 0.25, scored
    assert float(scored["recency_days"]) == 12.5, scored
    assert scored["churn_as_of"] == ids["today"], scored
    assert scored["churn_activity"] == CHURN_ACTIVITY, scored
    assert int(scored["churn_horizon_days"]) == CHURN_HORIZON, scored
    assert scored["churn_snapshot_id"] == ids["churn"], scored

    # A snapshot dated after today is not a fact the system has yet: the read
    # ignores it, so the band it carries never reaches a reader.
    assert ids["future"] not in {r["churn_snapshot_id"] for r in rows}

    # Not scored reads NULL, and the vintage still travels.
    assert unscored["band"] is None, unscored
    assert unscored["p_alive"] is None, unscored
    assert unscored["churn_as_of"] == ids["today"], unscored


async def test_both_arms_bind_the_business_date() -> None:
    """The vintage bound is in the SQL, not only in the seed's dates.

    A future-dated snapshot is inserted by the test above, so the read has to
    refuse it in the query itself; the bound is written as the +08 business
    date, because a bare `now()::date` would be the server's day.
    """
    for name in ("page_clv_scores", "page_churn_scores"):
        sql = _statement(name)
        assert "AT TIME ZONE interval '8 hours'" in sql, name
        assert "as_of <=" in sql, name
