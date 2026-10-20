"""Recommendation attribution: a served recommendation, answered by a line.

`rec_clicks` is written by the server that SERVES a recommendation (app);
data writes the attribution at apply time, per ORDER LINE. The rules this
file holds, each with the failure it prevents:

- a line with no token writes NO attribution row - absence is normal, and
  a reader must say "no attributed revenue yet", never $0;
- a token that names no served recommendation is quarantined, not ignored;
- a token whose product or customer is not the line's is quarantined with
  BOTH values named - never attributed to the wrong thing;
- two lines of one order carry two attributions - per line, not per order.

Rows written here live in this module's own keyspace and are purged at
module teardown (the suite shares one database across files and is not
transactional).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "run_upload", REPO / "scripts" / "run_upload.py")
assert _spec is not None and _spec.loader is not None
run_upload = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_upload)

BRAND_ID = "60100000-0000-0000-0000-000000000001"
STORE_CODE = "RECATTR-S1"
PRODUCT_SKU = "RECATTR-SKU"
OTHER_SKU = "RECATTR-SKU2"
CUSTOMER_KEYS = "M-9001%"
BATCH_ALGOS = "rec-attr-test"
ORDER_KEYS = "RA-%"

_UPLOAD_FILES = ("ra1.csv", "ra2.csv", "ra3.csv", "ra4.csv", "ra5.csv",
                 "ra6.csv", "ra7.csv")


async def _purge(dsn: str, since: datetime) -> list[str]:
    """Delete this module's rows; return any upload job it did not name."""
    import asyncpg

    pool = await asyncpg.create_pool(dsn)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM upload_jobs WHERE filename = ANY($2::text[])"
                " AND created_at >= $1", since, list(_UPLOAD_FILES))
            await conn.execute(
                "DELETE FROM order_recommendations WHERE rec_ref IN"
                " (SELECT rec_ref FROM rec_clicks WHERE batch_id IN"
                "  (SELECT id FROM rec_batches WHERE algos = $1))", BATCH_ALGOS)
            await conn.execute(
                "DELETE FROM order_items WHERE order_id IN"
                " (SELECT id FROM orders WHERE external_order_id LIKE $1)",
                ORDER_KEYS)
            await conn.execute(
                "DELETE FROM orders WHERE external_order_id LIKE $1", ORDER_KEYS)
            await conn.execute(
                "DELETE FROM rec_clicks WHERE batch_id IN"
                " (SELECT id FROM rec_batches WHERE algos = $1)", BATCH_ALGOS)
            await conn.execute(
                "DELETE FROM rec_batches WHERE algos = $1", BATCH_ALGOS)
            # customers cascade to customer_events (the derived order_placed
            # row) and customer_contacts.
            await conn.execute(
                "DELETE FROM customers WHERE member_id LIKE $1", CUSTOMER_KEYS)
            await conn.execute(
                "DELETE FROM products WHERE sku LIKE 'RECATTR-%'")
            await conn.execute(
                "DELETE FROM stores WHERE code LIKE 'RECATTR-%'")
            return [r["filename"] for r in await conn.fetch(
                "SELECT filename FROM upload_jobs WHERE created_at >= $1", since)]
    finally:
        await pool.close()


@pytest.fixture(scope="module", autouse=True)
def _no_residue(database_url: str) -> Iterator[None]:
    """Leave the cluster as it was found, and fail here if this module did not.

    A job left behind answers `reused: true` on the next run and applies
    nothing, so the next run would measure a file it never read.
    """
    started = datetime.now(timezone.utc)
    yield
    stray = asyncio.run(_purge(database_url, started))
    assert not stray, (
        f"upload job(s) left under a filename missing from `_UPLOAD_FILES`: "
        f"{stray}")


async def _seed(conn, member_id: str) -> None:  # type: ignore[no-untyped-def]
    """The store, the two products and the customer this module writes with."""
    await conn.execute(
        "INSERT INTO stores (code, name, brand_id)"
        " VALUES ($1, 'Rec Attr Store', $2::uuid)"
        " ON CONFLICT (code) DO NOTHING", STORE_CODE, BRAND_ID)
    for sku in (PRODUCT_SKU, OTHER_SKU):
        await conn.execute(
            "INSERT INTO products (sku, name, category, price_cents)"
            " VALUES ($1, 'Rec Attr Widget', 'frames', 100)"
            " ON CONFLICT (sku) DO NOTHING", sku)
    await conn.execute(
        "INSERT INTO customers (member_id, full_name) VALUES ($1, 'Rec Attr Buyer')"
        " ON CONFLICT (member_id) DO NOTHING", member_id)


async def _batch(conn) -> str:  # type: ignore[no-untyped-def]
    """A recommendation batch, as the batch writer would leave one."""
    return str(await conn.fetchval(
        "INSERT INTO rec_batches (algos, params, customer_count, item_count)"
        " VALUES ($1, '{}'::jsonb, 1, 1) RETURNING id", BATCH_ALGOS))


async def _click(conn, batch_id: str, member_id: str, sku: str) -> str:  # type: ignore[no-untyped-def]
    """A SERVED recommendation, written the way the serving app writes it."""
    customer_id = await conn.fetchval(
        "SELECT id FROM customers WHERE member_id = $1", member_id)
    product_id = await conn.fetchval(
        "SELECT id FROM products WHERE sku = $1", sku)
    return str(await conn.fetchval(
        "INSERT INTO rec_clicks (batch_id, customer_id, product_id, rank)"
        " VALUES ($1, $2, $3, 1) RETURNING rec_ref",
        batch_id, customer_id, product_id))


def _order_csv(tmp_path: Path, name: str,
               rows: list[tuple[str, str, list[dict]]]) -> Path:
    """One orders CSV: (external_order_id, member_id, lines) per row."""
    out = ["store_id,external_order_id,customer_id,lines"]
    for external_id, member_id, lines in rows:
        payload = json.dumps(lines).replace('"', '""')
        out.append(f'{STORE_CODE},{external_id},{member_id},"{payload}"')
    path = tmp_path / name
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


async def _quarantine(database_url: str, job_id: str, row_number: int):
    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT code, reason FROM quarantine_rows"
                " WHERE upload_job_id = $1 AND row_number = $2",
                job_id, row_number)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_served_recommendation_is_attributed_to_its_line(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """The token names what was served, and the line answers it."""
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            await _seed(conn, "M-900101")
            batch_id = await _batch(conn)
            rec_ref = await _click(conn, batch_id, "M-900101", PRODUCT_SKU)
    finally:
        await pool.close()

    csv = _order_csv(tmp_path, "ra1.csv", [("RA-1", "M-900101", [
        {"sku": PRODUCT_SKU, "qty": 1, "unit_cents": 100,
         "rec_ref": rec_ref}])])
    out = await run_upload.run(
        csv, "orders", "rec-attr", None, None, None, apply=True)
    assert out["status"] == "committed", out
    assert out["committed"] == 1 and out["quarantined"] == 0, out

    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT orr.rec_ref, rc.product_id, rc.customer_id,"
                " rc.batch_id, rc.rank, oi.product_id AS line_product"
                " FROM orders o JOIN order_items oi ON oi.order_id = o.id"
                " JOIN order_recommendations orr"
                "   ON orr.order_item_id = oi.id"
                " JOIN rec_clicks rc ON rc.rec_ref = orr.rec_ref"
                " WHERE o.external_order_id = 'RA-1'")
            assert row is not None, "the line carries no attribution row"
            assert str(row["rec_ref"]) == rec_ref
            assert str(row["line_product"]) == str(row["product_id"])
            customer_id = await conn.fetchval(
                "SELECT id FROM customers WHERE member_id = 'M-900101'")
            assert str(row["customer_id"]) == str(customer_id)
            assert int(row["rank"]) == 1
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_line_without_a_token_writes_no_attribution(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """Absence is normal: the order lands, the attribution table stays empty."""
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            await _seed(conn, "M-900102")
    finally:
        await pool.close()

    csv = _order_csv(tmp_path, "ra2.csv", [("RA-2", "M-900102", [
        {"sku": PRODUCT_SKU, "qty": 1, "unit_cents": 100}])])
    out = await run_upload.run(
        csv, "orders", "rec-attr", None, None, None, apply=True)
    assert out["status"] == "committed", out
    assert out["committed"] == 1 and out["quarantined"] == 0, out

    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            landed = await conn.fetchval(
                "SELECT count(*) FROM orders WHERE external_order_id = 'RA-2'")
            assert int(landed) == 1
            attributed = await conn.fetchval(
                "SELECT count(*) FROM order_recommendations orr"
                " JOIN order_items oi ON oi.id = orr.order_item_id"
                " JOIN orders o ON o.id = oi.order_id"
                " WHERE o.external_order_id = 'RA-2'")
            assert int(attributed) == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_token_for_another_product_is_quarantined_with_both_values(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """Never attribute a recommendation to a different product."""
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            await _seed(conn, "M-900103")
            batch_id = await _batch(conn)
            rec_ref = await _click(conn, batch_id, "M-900103", PRODUCT_SKU)
            served_product = await conn.fetchval(
                "SELECT id FROM products WHERE sku = $1", PRODUCT_SKU)
            line_product = await conn.fetchval(
                "SELECT id FROM products WHERE sku = $1", OTHER_SKU)
    finally:
        await pool.close()

    csv = _order_csv(tmp_path, "ra3.csv", [("RA-3", "M-900103", [
        {"sku": OTHER_SKU, "qty": 1, "unit_cents": 100,
         "rec_ref": rec_ref}])])
    out = await run_upload.run(
        csv, "orders", "rec-attr", None, None, None, apply=True)
    assert out["status"] == "committed_with_rejects", out
    assert out["committed"] == 0 and out["quarantined"] == 1, out

    row = await _quarantine(database_url, out["job_id"], 1)
    assert row is not None, "the mismatch was dropped instead of quarantined"
    assert row["code"] == "rec_product_mismatch", row
    assert str(served_product) in row["reason"], row
    assert str(line_product) in row["reason"], row

    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            landed = await conn.fetchval(
                "SELECT count(*) FROM orders WHERE external_order_id = 'RA-3'")
            assert int(landed) == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_token_that_names_nothing_is_quarantined(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """A token no served recommendation answers is a disagreement, not a no-op."""
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            await _seed(conn, "M-900104")
    finally:
        await pool.close()

    unknown = str(uuid.uuid4())
    csv = _order_csv(tmp_path, "ra4.csv", [("RA-4", "M-900104", [
        {"sku": PRODUCT_SKU, "qty": 1, "unit_cents": 100,
         "rec_ref": unknown}])])
    out = await run_upload.run(
        csv, "orders", "rec-attr", None, None, None, apply=True)
    assert out["status"] == "committed_with_rejects", out
    assert out["committed"] == 0 and out["quarantined"] == 1, out

    row = await _quarantine(database_url, out["job_id"], 1)
    assert row is not None
    assert row["code"] == "rec_ref_unknown", row
    assert unknown in row["reason"], row


@pytest.mark.asyncio
async def test_a_token_served_to_another_customer_is_quarantined(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """The order's customer must be the customer the recommendation was served to."""
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            await _seed(conn, "M-900105")
            await _seed(conn, "M-900106")
            batch_id = await _batch(conn)
            rec_ref = await _click(conn, batch_id, "M-900105", PRODUCT_SKU)
            served_to = await conn.fetchval(
                "SELECT id FROM customers WHERE member_id = 'M-900105'")
            bought_by = await conn.fetchval(
                "SELECT id FROM customers WHERE member_id = 'M-900106'")
    finally:
        await pool.close()

    csv = _order_csv(tmp_path, "ra5.csv", [("RA-5", "M-900106", [
        {"sku": PRODUCT_SKU, "qty": 1, "unit_cents": 100,
         "rec_ref": rec_ref}])])
    out = await run_upload.run(
        csv, "orders", "rec-attr", None, None, None, apply=True)
    assert out["status"] == "committed_with_rejects", out
    assert out["committed"] == 0 and out["quarantined"] == 1, out

    row = await _quarantine(database_url, out["job_id"], 1)
    assert row is not None
    assert row["code"] == "rec_customer_mismatch", row
    assert str(served_to) in row["reason"], row
    assert str(bought_by) in row["reason"], row


@pytest.mark.asyncio
async def test_a_malformed_token_is_rejected_before_it_can_be_written(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """A token that is not a uuid is a rejected row, not a dropped attribution."""
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            await _seed(conn, "M-900107")
    finally:
        await pool.close()

    csv = _order_csv(tmp_path, "ra6.csv", [("RA-6", "M-900107", [
        {"sku": PRODUCT_SKU, "qty": 1, "unit_cents": 100,
         "rec_ref": "not-a-uuid"}])])
    out = await run_upload.run(
        csv, "orders", "rec-attr", None, None, None, apply=True)
    assert out["status"] == "committed_with_rejects", out
    assert out["committed"] == 0 and out["quarantined"] == 1, out

    row = await _quarantine(database_url, out["job_id"], 1)
    assert row is not None
    assert row["code"] == "rec_ref_invalid", row

    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            landed = await conn.fetchval(
                "SELECT count(*) FROM orders WHERE external_order_id = 'RA-6'")
            assert int(landed) == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_two_lines_two_tokens_are_two_attributions(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """The attribution is per ORDER LINE: one order, two recommendations."""
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            await _seed(conn, "M-900108")
            batch_id = await _batch(conn)
            first = await _click(conn, batch_id, "M-900108", PRODUCT_SKU)
            second = await _click(conn, batch_id, "M-900108", OTHER_SKU)
    finally:
        await pool.close()

    csv = _order_csv(tmp_path, "ra7.csv", [("RA-7", "M-900108", [
        {"sku": PRODUCT_SKU, "qty": 1, "unit_cents": 100,
         "rec_ref": first},
        {"sku": OTHER_SKU, "qty": 2, "unit_cents": 100,
         "rec_ref": second}])])
    out = await run_upload.run(
        csv, "orders", "rec-attr", None, None, None, apply=True)
    assert out["status"] == "committed", out
    assert out["committed"] == 1 and out["quarantined"] == 0, out

    pool = await asyncpg.create_pool(database_url)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT orr.rec_ref, oi.product_id FROM orders o"
                " JOIN order_items oi ON oi.order_id = o.id"
                " JOIN order_recommendations orr"
                "   ON orr.order_item_id = oi.id"
                " WHERE o.external_order_id = 'RA-7'")
            assert len(rows) == 2, rows
            assert {str(r["rec_ref"]) for r in rows} == {first, second}
            assert len({str(r["product_id"]) for r in rows}) == 2
    finally:
        await pool.close()
