"""Revenue at risk (A1/F4): the money a customer costs if they churn.

The churn screen ranks by probability; this is the value dimension it never
joined: revenue_at_risk_cents = (1 - p_alive) * clv_cents over the newest
churn snapshot and the newest CLV snapshot in the page's own scope. Two
rules make the number honest, and this file holds both:

- the JOIN never invents money: a customer missing from either snapshot
  reads NULL, never $0, and each side's vintage travels with the row;
- the SCOPE is each snapshot's own stamp (brand_id, group_id): a brand
  session never reads another brand's risk, and the triage list excludes
  soft-deleted customers even when the snapshot still carries them.

This file writes into its OWN brand and deletes every row it created in a
`finally`, so a shared-database run leaves the row counts where it found them.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Iterator

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

DATA_ROOT = pathlib.Path(__file__).resolve().parents[3]
QUERIES = DATA_ROOT / "db" / "queries" / "revenue_at_risk.sql"

GROUP_ID = "c0c00000-0000-0000-0000-0000000000e1"
BRAND_A = "b0b00000-0000-0000-0000-0000000000e1"
BRAND_B = "b0b00000-0000-0000-0000-0000000000e2"
RICH_MEMBER = "M-900601"
POOR_MEMBER = "M-900602"
UNCLV_MEMBER = "M-900603"
GONE_MEMBER = "M-900604"

CLV_HORIZON = 90
CHURN_ACTIVITY = "orders"
CHURN_HORIZON = 90


def _statement(name: str) -> str:
    """The named statement out of db/queries/revenue_at_risk.sql."""
    text = QUERIES.read_text(encoding="utf-8")
    chunks = text.split("-- name: ")
    for chunk in chunks[1:]:
        head, _, body = chunk.partition("\n")
        if head.strip() == name:
            return "\n".join(
                ln for ln in body.splitlines()
                if not ln.strip().startswith(("-- name:", "-- params:")))
    raise AssertionError(f"{name} is not in {QUERIES}")


async def _wipe(conn) -> None:  # type: ignore[no-untyped-def]
    """This file's rows, and nothing else: scores, snapshots, customers."""
    await conn.execute(
        "DELETE FROM clv_customer_scores WHERE snapshot_id IN"
        " (SELECT id FROM clv_snapshots WHERE brand_id = ANY($1::uuid[]))",
        [BRAND_A, BRAND_B])
    await conn.execute(
        "DELETE FROM churn_customer_scores WHERE snapshot_id IN"
        " (SELECT id FROM churn_snapshots WHERE brand_id = ANY($1::uuid[]))",
        [BRAND_A, BRAND_B])
    await conn.execute(
        "DELETE FROM clv_snapshots WHERE brand_id = ANY($1::uuid[])",
        [BRAND_A, BRAND_B])
    await conn.execute(
        "DELETE FROM churn_snapshots WHERE brand_id = ANY($1::uuid[])",
        [BRAND_A, BRAND_B])
    await conn.execute(
        "DELETE FROM customers WHERE member_id = ANY($1::text[])",
        [RICH_MEMBER, POOR_MEMBER, UNCLV_MEMBER, GONE_MEMBER])
    await conn.execute(
        "DELETE FROM brands WHERE id = ANY($1::uuid[])", [BRAND_A, BRAND_B])
    await conn.execute("DELETE FROM groups WHERE id = $1::uuid", GROUP_ID)


async def _purge(dsn: str) -> None:
    """Delete this file's rows after the module run."""
    import asyncpg

    pool = await asyncpg.create_pool(dsn)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await _wipe(conn)
    finally:
        await pool.close()


@pytest_asyncio.fixture()
async def db_pool(database_url: str):
    """Function-loop pool (same shape as test_customer_scores_read)."""
    import asyncpg

    own = await asyncpg.create_pool(database_url)
    yield own
    await own.close()


@pytest.fixture(scope="module", autouse=True)
def _no_residue(database_url: str) -> Iterator[None]:
    yield
    asyncio.run(_purge(database_url))


async def _seed(conn) -> dict:  # type: ignore[no-untyped-def]
    """One brand with four customers: rich risk, poor risk, half-scored,
    and soft-deleted; plus another brand's snapshot for the isolation read."""
    today = await conn.fetchval(
        "SELECT (now() AT TIME ZONE interval '8 hours')::date")

    await _wipe(conn)
    await conn.execute(
        "INSERT INTO groups (id, code, name) VALUES ($1::uuid, 'RAR-GRP',"
        " 'Revenue Risk Group') ON CONFLICT (id) DO NOTHING", GROUP_ID)
    for brand, code in ((BRAND_A, "RAR-BRND-A"), (BRAND_B, "RAR-BRND-B")):
        await conn.execute(
            "INSERT INTO brands (id, group_id, code, name)"
            " VALUES ($1::uuid, $2::uuid, $3, $4)"
            " ON CONFLICT (id) DO NOTHING", brand, GROUP_ID, code, code)
    for member_id in (RICH_MEMBER, POOR_MEMBER, UNCLV_MEMBER, GONE_MEMBER):
        await conn.execute(
            "INSERT INTO customers (member_id, full_name, group_id)"
            " VALUES ($1, $2, $3::uuid) ON CONFLICT (member_id) DO NOTHING",
            member_id, member_id, GROUP_ID)
    ids = {}
    for member_id in (RICH_MEMBER, POOR_MEMBER, UNCLV_MEMBER, GONE_MEMBER):
        ids[member_id] = await conn.fetchval(
            "SELECT id FROM customers WHERE member_id = $1", member_id)
    await conn.execute(
        "UPDATE customers SET deleted_at = now() WHERE member_id = $1",
        GONE_MEMBER)

    clv = await conn.fetchval(
        "INSERT INTO clv_snapshots (as_of, horizon_days, brand_id,"
        " customer_count, created_by)"
        " VALUES ($1::date, $2::int, $3::uuid, 4, 'test') RETURNING id",
        today, CLV_HORIZON, BRAND_A)
    # p_alive is stored on both score rows; the CLV side's is not read.
    for member_id, cents, tier in ((RICH_MEMBER, 100000.0, "high"),
                                   (POOR_MEMBER, 2000.0, "low"),
                                   (GONE_MEMBER, 50000.0, "mid")):
        await conn.execute(
            "INSERT INTO clv_customer_scores (snapshot_id, customer_id, p_alive,"
            " expected_purchases, avg_order_value_cents, clv_cents, value_tier)"
            " VALUES ($1::uuid, $2::uuid, 1.0, 3.0, 500.0, $3, $4)",
            clv, ids[member_id], cents, tier)
    churn = await conn.fetchval(
        "INSERT INTO churn_snapshots (as_of, activity, horizon_days, brand_id,"
        " customer_count, created_by)"
        " VALUES ($1::date, $2::text, $3::int, $4::uuid, 4, 'test')"
        " RETURNING id", today, CHURN_ACTIVITY, CHURN_HORIZON, BRAND_A)
    for member_id, p_alive, band in ((RICH_MEMBER, 0.20, "at-risk"),
                                     (POOR_MEMBER, 0.90, "cooling"),
                                     (UNCLV_MEMBER, 0.10, "at-risk"),
                                     (GONE_MEMBER, 0.05, "at-risk")):
        await conn.execute(
            "INSERT INTO churn_customer_scores (snapshot_id, customer_id,"
            " recency_days, p_alive, expected_purchases, band)"
            " VALUES ($1::uuid, $2::uuid, 10.0, $3, 1.0, $4)",
            churn, ids[member_id], p_alive, band)
    # Another brand's money: the page must never read it.
    other_clv = await conn.fetchval(
        "INSERT INTO clv_snapshots (as_of, horizon_days, brand_id,"
        " customer_count, created_by)"
        " VALUES ($1::date, $2::int, $3::uuid, 0, 'test') RETURNING id",
        today, CLV_HORIZON, BRAND_B)
    await conn.execute(
        "INSERT INTO clv_customer_scores (snapshot_id, customer_id, p_alive,"
        " expected_purchases, avg_order_value_cents, clv_cents, value_tier)"
        " VALUES ($1::uuid, $2::uuid, 1.0, 3.0, 500.0, 777777.0, 'high')",
        other_clv, ids[RICH_MEMBER])
    other_churn = await conn.fetchval(
        "INSERT INTO churn_snapshots (as_of, activity, horizon_days, brand_id,"
        " customer_count, created_by)"
        " VALUES ($1::date, $2::text, $3::int, $4::uuid, 0, 'test')"
        " RETURNING id", today, CHURN_ACTIVITY, CHURN_HORIZON, BRAND_B)
    await conn.execute(
        "INSERT INTO churn_customer_scores (snapshot_id, customer_id,"
        " recency_days, p_alive, expected_purchases, band)"
        " VALUES ($1::uuid, $2::uuid, 1.0, 0.01, 1.0, 'at-risk')",
        other_churn, ids[RICH_MEMBER])

    return {"ids": ids, "clv": clv, "churn": churn, "today": today}


async def test_the_page_joins_money_to_risk_and_reads_null_not_zero(
        db_pool) -> None:
    """(1-p_alive) * clv_cents per customer, with both vintages attached."""
    sql = _statement("revenue_at_risk_page")
    async with db_pool.acquire() as conn:
        ids = await _seed(conn)
        by_member = ids["ids"]
        rows = await conn.fetch(
            sql, [by_member[RICH_MEMBER], by_member[POOR_MEMBER],
                  by_member[UNCLV_MEMBER]],
            BRAND_A, None, CHURN_ACTIVITY, CHURN_HORIZON, CLV_HORIZON)

    assert len(rows) == 3, rows
    by_customer = {r["customer_id"]: r for r in rows}
    rich = by_customer[by_member[RICH_MEMBER]]
    # (1 - 0.20) * 100000 = 80000: the money, not the probability.
    assert int(rich["revenue_at_risk_cents"]) == 80000, rich
    assert rich["churn_band"] == "at-risk", rich
    assert float(rich["churn_p_alive"]) == 0.20, rich
    assert float(rich["clv_cents"]) == 100000.0, rich
    assert rich["clv_tier"] == "high", rich
    assert rich["churn_snapshot_id"] == ids["churn"], rich
    assert rich["clv_snapshot_id"] == ids["clv"], rich
    assert rich["churn_as_of"] == ids["today"], rich
    assert rich["clv_as_of"] == ids["today"], rich
    assert rich["churn_activity"] == CHURN_ACTIVITY, rich
    poor = by_customer[by_member[POOR_MEMBER]]
    assert int(poor["revenue_at_risk_cents"]) == 200, poor
    # Half-scored: the churn side exists but the CLV side does not, so the
    # money is NULL -- never $0 -- while both vintages still travel (the
    # CLV snapshot exists in this scope; this customer is just not in it).
    half = by_customer[by_member[UNCLV_MEMBER]]
    assert half["revenue_at_risk_cents"] is None, half
    assert half["churn_band"] == "at-risk", half
    assert half["churn_snapshot_id"] == ids["churn"], half
    assert half["clv_snapshot_id"] == ids["clv"], half
    assert half["clv_cents"] is None, half
    assert half["clv_tier"] is None, half


async def test_the_page_never_reads_another_brand(db_pool) -> None:
    """Brand B's 777777 stays in brand B, even for the same customer."""
    sql = _statement("revenue_at_risk_page")
    async with db_pool.acquire() as conn:
        ids = await _seed(conn)
        rows = await conn.fetch(
            sql, [ids["ids"][RICH_MEMBER]],
            BRAND_A, None, CHURN_ACTIVITY, CHURN_HORIZON, CLV_HORIZON)

    assert len(rows) == 1, rows
    assert int(rows[0]["revenue_at_risk_cents"]) == 80000, rows[0]


async def test_the_top_list_ranks_by_money_and_skips_the_deleted(
        db_pool) -> None:
    """Richest risk first; the soft-deleted customer never enumerates."""
    sql = _statement("revenue_at_risk_top")
    async with db_pool.acquire() as conn:
        ids = await _seed(conn)
        rows = await conn.fetch(
            sql, BRAND_A, None, CHURN_ACTIVITY, CHURN_HORIZON,
            CLV_HORIZON, 10, None)

    # GONE_MEMBER (0.95 * 50000 = 47500) would rank second but is deleted,
    # so the list is rich (80000) then poor (200); the half-scored customer
    # carries no CLV row and cannot rank.
    got = [r["customer_id"] for r in rows]
    assert got == [ids["ids"][RICH_MEMBER], ids["ids"][POOR_MEMBER]], rows
    assert int(rows[0]["revenue_at_risk_cents"]) == 80000, rows[0]
    assert int(rows[1]["revenue_at_risk_cents"]) == 200, rows[1]

    async with db_pool.acquire() as conn:
        at_risk = await conn.fetch(
            sql, BRAND_A, None, CHURN_ACTIVITY, CHURN_HORIZON,
            CLV_HORIZON, 10, "at-risk")
        cooling = await conn.fetch(
            sql, BRAND_A, None, CHURN_ACTIVITY, CHURN_HORIZON,
            CLV_HORIZON, 10, "cooling")
        limited = await conn.fetch(
            sql, BRAND_A, None, CHURN_ACTIVITY, CHURN_HORIZON,
            CLV_HORIZON, 1, None)

    assert [r["customer_id"] for r in at_risk] == [ids["ids"][RICH_MEMBER]]
    assert [r["customer_id"] for r in cooling] == [ids["ids"][POOR_MEMBER]]
    assert [r["customer_id"] for r in limited] == [ids["ids"][RICH_MEMBER]]
