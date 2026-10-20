"""Core-table apply tests: staged ok rows -> core tables, idempotently.

End-to-end through scripts/run_upload.run(..., apply=True) against the
session pgembed DB (unique keys per test; shared DB across files).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
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


# Every row this module writes, by its namespace. The suite shares one database
# across files and is not transactional, so a fixture that leaves a row behind
# leaves it for every other reader: an `APPLY-SKU` product with category 'test'
# is a sixth feature dimension in the next segment fit (contracts.md § 015),
# and a 2099 order makes every churn horizon look observed (measured 2026-10-20:
# `churn_eval.window_is_observed` read true for all 20 stored snapshots off one
# future row). The purge names the keyspaces; the assertion below is what fails
# when a future edit writes a row this list does not name.
CUSTOMER_KEYS = "M-9000%"

# Every dependent of `customers`, discovered rather than listed: the first
# version named four children and died on `customer_contacts`, which is what a
# hand-written list does when a later migration adds a table.
DEPENDENTS_SQL = """
    select tc.table_name, kcu.column_name
    from information_schema.table_constraints tc
    join information_schema.key_column_usage kcu
      on kcu.constraint_name = tc.constraint_name
    join information_schema.constraint_column_usage ccu
      on ccu.constraint_name = tc.constraint_name
    where tc.constraint_type = 'FOREIGN KEY'
      and ccu.table_name = $1 and tc.table_name <> $1
"""


async def _children(conn, table: str, ids: list, skip: tuple = ()) -> int:
    """Delete every row pointing at `ids`, whatever the FK is called."""
    removed = 0
    for row in await conn.fetch(DEPENDENTS_SQL, table):
        if row["table_name"] in skip:
            continue
        tag = await conn.execute(
            f'DELETE FROM "{row["table_name"]}" '
            f'WHERE "{row["column_name"]}" = ANY($1::uuid[])', ids)
        removed += int(tag.rsplit(" ", 1)[-1])
    return removed


async def _purge(dsn: str, since: datetime) -> tuple[int, int]:
    """Delete this module's rows; return (rows removed, future orders left).

    `since` keeps the delete to this module's run and `_UPLOAD_FILES` keeps it
    to this module's namespace -- `run_upload` dedups on `(filename, file_hash)`,
    so a job left behind makes the *next* run return `reused: true` with no
    apply at all -- measured 2026-10-20: 31 tests failed in a cascade from
    `KeyError: 'committed'`, every one on a reused job whose hash an earlier run
    had stored. Narrowed after review: a time window alone would delete a
    developer's in-flight upload on a shared cluster, so the names do the
    scoping and `since` only guards.
    """
    import asyncpg

    pool = await asyncpg.create_pool(dsn)
    assert pool is not None
    removed = 0
    try:
        async with pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM upload_jobs"
                " WHERE filename = ANY($2::text[]) AND created_at >= $1",
                since, list(_UPLOAD_FILES))
            removed += int(tag.rsplit(" ", 1)[-1])
            ids = [r["id"] for r in await conn.fetch(
                "SELECT id FROM customers WHERE member_id LIKE $1", CUSTOMER_KEYS)]
            if ids:
                removed += await _children(conn, "customers", ids, skip=("orders",))
                orders = [r["id"] for r in await conn.fetch(
                    "SELECT id FROM orders WHERE customer_id = ANY($1::uuid[])",
                    ids)]
                if orders:
                    removed += await _children(conn, "orders", orders)
                    tag = await conn.execute(
                        "DELETE FROM orders WHERE id = ANY($1::uuid[])", orders)
                    removed += int(tag.rsplit(" ", 1)[-1])
                tag = await conn.execute(
                    "DELETE FROM customers WHERE id = ANY($1::uuid[])", ids)
                removed += int(tag.rsplit(" ", 1)[-1])
            # The fixture's own product and store, and anything still pointing
            # at them: an earlier run's order_items kept the product alive after
            # its order was gone, which is exactly the "delete the parent, find
            # the child" order this list exists to get right.
            for statement in (
                    "DELETE FROM order_items WHERE product_id IN"
                    " (SELECT id FROM products WHERE sku LIKE 'APPLY-%')",
                    "DELETE FROM order_custom_data WHERE order_id IN (SELECT id"
                    " FROM orders WHERE store_id IN (SELECT id FROM stores"
                    " WHERE code LIKE 'APPLY-%'))",
                    "DELETE FROM order_items WHERE order_id IN (SELECT id FROM"
                    " orders WHERE store_id IN (SELECT id FROM stores"
                    " WHERE code LIKE 'APPLY-%'))",
                    "DELETE FROM orders WHERE store_id IN (SELECT id FROM"
                    " stores WHERE code LIKE 'APPLY-%')",
                    "DELETE FROM products WHERE sku LIKE 'APPLY-%'",
                    "DELETE FROM stores WHERE code LIKE 'APPLY-%'"):
                tag = await conn.execute(statement)
                removed += int(tag.rsplit(" ", 1)[-1])
            # The refusals this module caused. They cascade from `upload_jobs`,
            # but nothing links a quarantine row to the order it refused, so the
            # match is the id namespace: every order id here starts `E-`. A
            # revoked DELETE on the table is about app-facing roles writing
            # audit -- a rehearsal's own refusals are debris, and leaving them
            # makes a later reader count them as product.
            tag = await conn.execute(
                "DELETE FROM quarantine_rows WHERE key LIKE 'E-%'"
                " OR raw->>'external_order_id' LIKE 'E-%'")
            removed += int(tag.rsplit(" ", 1)[-1])
            future = int(await conn.fetchval(
                "SELECT count(*) FROM orders WHERE ordered_at > now()"))
            # The guard the namespace delete needs: `since` is the only thing
            # left that can hold a row this module wrote under a filename the
            # list does not know, so name it rather than pass.
            stray = [r["filename"] for r in await conn.fetch(
                "SELECT filename FROM upload_jobs WHERE created_at >= $1",
                since)]
    finally:
        await pool.close()
    return removed, future, stray


_UPLOAD_FILES = (
    "badts.csv", "c1.csv", "c2.csv", "c9.csv", "e1.csv", "e2.csv",
    "future.csv", "i1.csv", "naivets.csv", "o1.csv", "o2.csv", "q.csv",
    "r1.csv", "r2.csv", "race.csv", "revive.csv", "revive_order.csv",
    "s1.csv", "s2.csv", "s3.csv", "s4.csv",
)


@pytest.fixture(scope="module", autouse=True)
def _no_residue(database_url: str) -> Iterator[None]:
    """Leave the cluster as it was found, and fail here if this module did not.

    Module-scoped rather than per test: the tests share the seeded store and
    product, so a purge between them would re-seed every time. The assertion
    after the purge is the point -- a new test that writes a row this list does
    not name fails next to its own file instead of in another repo's numerics.
    """
    started = datetime.now(timezone.utc)
    yield
    _, future, stray = asyncio.run(_purge(database_url, started))
    assert not stray, (
        f"upload job(s) left under a filename missing from `_UPLOAD_FILES`: "
        f"{stray} -- left behind, the next run's dedup answers `reused: true` "
        "and applies nothing, so the next run measures a file it never read")
    assert future == 0, (
        f"{future} future-dated order(s) left on the cluster after cleanup: a "
        "rehearsal row at 2099 makes every churn horizon read as observed and "
        "puts its customer above every real one on days-since-order")

async def _seed(conn) -> None:  # type: ignore[no-untyped-def]
    await conn.execute(
        "INSERT INTO stores (code, name, brand_id)"
        " VALUES ('APPLY-S1', 'Apply Store',"
        " '60100000-0000-0000-0000-000000000001')"
        " ON CONFLICT (code) DO NOTHING")
    await conn.execute(
        "INSERT INTO products (sku, name, category, price_cents)"
        " VALUES ('APPLY-SKU', 'Apply Widget', 'frames', 100)"
        " ON CONFLICT (sku) DO NOTHING")


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_rejection_codes_are_named_or_unclassified() -> None:
    """A code is a token, never the free-text reason, and never invented.

    `code_of` is the one place a rejection becomes a machine-readable bucket:
    a validator that names a code keeps it, anything else lands as `other` so
    an unclassified row is visible rather than merged into a plausible one.
    """
    from pipelines.ingest.errors import (RowMissing, RowRejected,
                                         code_of)

    assert code_of(RowRejected("bad yaml", "invalid_yaml")) == "invalid_yaml"
    assert code_of(RowMissing("no sku", "sku_missing")) == "sku_missing"
    assert code_of(RowRejected("hand-written")) == "other"
    assert code_of(ValueError("hand-written")) == "other"


@pytest.mark.asyncio
async def test_apply_customers_upsert_and_contacts(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    csv = ("member_id,full_name,external_member_id,phone,email\n"
           "M-900001,Apply One,EXT-A1,5550001,one@example.com\n")
    out = await run_upload.run(
        _write(tmp_path, "c1.csv", csv), "customers", "pos", None,
        None, None, apply=True)
    assert out["status"] == "committed", out
    assert out["committed"] == 1 and out["quarantined"] == 0
    assert out["quarantine_reasons"] == {}, out

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await _seed(conn)
            name = await conn.fetchval(
                "SELECT full_name FROM customers WHERE member_id='M-900001'")
            assert name == "Apply One"
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM customer_contacts cc JOIN customers c"
                " ON c.id = cc.customer_id WHERE c.member_id = 'M-900001'")
            assert int(n) == 2
    finally:
        await pool.close()

    # second-see update converges; re-upload = same job (no dupes)
    csv2 = ("member_id,full_name\nM-900001,Apply One Renamed\n")
    out2 = await run_upload.run(
        _write(tmp_path, "c2.csv", csv2), "customers", "pos", None,
        None, None, apply=True)
    assert out2["committed"] == 1
    out3 = await run_upload.run(
        _write(tmp_path, "c1.csv", csv), "customers", "pos", None,
        None, None, apply=True)
    assert out3["reused"] is True


@pytest.mark.asyncio
async def test_apply_orders_with_failures_and_replay(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await _seed(conn)
            await conn.execute(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ('M-900002', 'Order Buyer')"
                " ON CONFLICT (member_id) DO NOTHING")
    finally:
        await pool.close()

    lines = json.dumps([{"sku": "APPLY-SKU", "qty": 2,
                         "unit_cents": 100}]).replace('"', '""')
    csv = ("store_id,external_order_id,customer_id,lines,ordered_at\n"
           f"APPLY-S1,E-A1,M-900002,\"{lines}\",2026-09-01T10:00:00Z\n"
           f"NOSUCHSTORE,E-A2,M-900002,\"{lines}\",\n")
    out = await run_upload.run(
        _write(tmp_path, "o1.csv", csv), "orders", "pos", None,
        None, None, apply=True)
    assert out["status"] == "committed_with_rejects", out
    assert out["total"] == 2
    assert out["committed"] == 1 and out["quarantined"] == 1
    assert out["apply_failed"] == [2]

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT total_cents FROM orders WHERE external_order_id='E-A1'")
            assert int(total) == 200
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM order_items oi JOIN orders o"
                " ON o.id = oi.order_id WHERE o.external_order_id = 'E-A1'")
            assert int(n) == 1
            reason = await conn.fetchval(
                "SELECT reason FROM quarantine_rows WHERE upload_job_id = $1"
                " AND row_number = 2", out["job_id"])
            assert reason is not None and "NOSUCHSTORE" in reason
            nev = await conn.fetchval(
                "SELECT COUNT(*) FROM customer_events ce JOIN orders o"
                " ON o.id::text = substr(ce.source_event_id, 7)"
                " WHERE o.external_order_id = 'E-A1'"
                " AND ce.kind = 'order_placed' AND ce.source = 'derived'")
            assert int(nev) == 1
    finally:
        await pool.close()

    # replay same order in a NEW file -> dedup skip, no second row
    csv_re = ("store_id,external_order_id,customer_id,lines\n"
              f"APPLY-S1,E-A1,M-900002,\"{lines}\"\n")
    out_re = await run_upload.run(
        _write(tmp_path, "o2.csv", csv_re), "orders", "pos", None,
        None, None, apply=True)
    assert out_re["committed"] == 1 and out_re["skipped"] == 1, out_re

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE external_order_id='E-A1'")
            assert int(n) == 1
            nev = await conn.fetchval(
                "SELECT COUNT(*) FROM customer_events ce JOIN orders o"
                " ON o.id::text = substr(ce.source_event_id, 7)"
                " WHERE o.external_order_id = 'E-A1'"
                " AND ce.kind = 'order_placed' AND ce.source = 'derived'")
            assert int(nev) == 1  # replay wrote no second derived event
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_apply_identity_and_events(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await _seed(conn)
            await conn.execute(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ('M-900003', 'Identity Target'),"
                "        ('M-900004', 'Event Target')"
                " ON CONFLICT (member_id) DO NOTHING")
    finally:
        await pool.close()

    out = await run_upload.run(
        _write(tmp_path, "i1.csv",
               "member_id,external_member_id,source\n"
               "M-900003,EXT-B1,pos\n"
               "M-999999,EXT-B2,pos\n"),
        "identity", "pos", None, None, None, apply=True)
    assert out["status"] == "committed_with_rejects", out
    assert out["committed"] == 1 and out["quarantined"] == 1

    out_e = await run_upload.run(
        _write(tmp_path, "e1.csv",
               "customer_id,type,source,source_event_id,summary\n"
               "M-900004,note,pos,EV-A1,hello\n"),
        "events", "pos", None, None, None, apply=True)
    assert out_e["status"] == "committed", out_e

    # replay event in a new file -> skip, still one row
    out_e2 = await run_upload.run(
        _write(tmp_path, "e2.csv",
               "customer_id,type,source,source_event_id\n"
               "M-900004,note,pos,EV-A1\n"),
        "events", "pos", None, None, None, apply=True)
    assert out_e2["skipped"] == 1, out_e2

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            ext = await conn.fetchval(
                "SELECT external_member_id FROM customers"
                " WHERE member_id = 'M-900003'")
            assert ext == "EXT-B1"
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM customer_events"
                " WHERE source = 'pos' AND source_event_id = 'EV-A1'")
            assert int(n) == 1
            summ = await conn.fetchval(
                "SELECT summary FROM customer_events"
                " WHERE source = 'pos' AND source_event_id = 'EV-A1'")
            assert summ == "hello"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_validate_only_default_unchanged(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """Without --apply: status validated, core tables untouched."""
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    csv = "member_id,full_name\nM-900005,Not Applied\n"
    out = await run_upload.run(
        _write(tmp_path, "c9.csv", csv), "customers", "pos", None,
        None, None)
    assert out["status"] == "validated", out
    assert out["committed"] == 1  # staged-ok; core apply not yet run

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM customers WHERE member_id='M-900005'")
            assert int(n) == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_ld4_reingest_keeps_stable_surrogate_ids(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """LD4 join-back probe: re-ingest of the same natural keys must keep
    the SAME surrogate PKs (join-back), not just avoid duplicates."""
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await _seed(conn)
    finally:
        await pool.close()

    csv_c = ("member_id,full_name\nM-900010,Stable Sam\n")
    out1 = await run_upload.run(
        _write(tmp_path, "s1.csv", csv_c), "customers", "pos", None,
        None, None, apply=True)
    assert out1["status"] == "committed", out1

    lines = json.dumps([{"sku": "APPLY-SKU", "qty": 1,
                         "unit_cents": 100}]).replace('"', '""')
    csv_o = ("store_id,external_order_id,customer_id,lines\n"
             f"APPLY-S1,E-STABLE,M-900010,\"{lines}\"\n")
    out2 = await run_upload.run(
        _write(tmp_path, "s2.csv", csv_o), "orders", "pos", None,
        None, None, apply=True)
    assert out2["status"] == "committed", out2

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            cid1 = await conn.fetchval(
                "SELECT id FROM customers WHERE member_id = 'M-900010'")
            oid1 = await conn.fetchval(
                "SELECT id FROM orders WHERE external_order_id = 'E-STABLE'")
    finally:
        await pool.close()

    # re-ingest: byte-different files (column reorder / name change ->
    # new upload jobs), same natural keys. Customer upsert changes the
    # name; the surrogate id must NOT move.
    csv_c2 = ("full_name,member_id\nStable Samuel,M-900010\n")
    csv_o2 = ("customer_id,store_id,external_order_id,lines\n"
              f"M-900010,APPLY-S1,E-STABLE,\"{lines}\"\n")
    out3 = await run_upload.run(
        _write(tmp_path, "s3.csv", csv_c2), "customers", "pos", None,
        None, None, apply=True)
    assert out3.get("reused") is not True, out3
    out4 = await run_upload.run(
        _write(tmp_path, "s4.csv", csv_o2), "orders", "pos", None,
        None, None, apply=True)
    assert out4.get("reused") is not True, out4

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            cid2 = await conn.fetchval(
                "SELECT id FROM customers WHERE member_id = 'M-900010'")
            oid2 = await conn.fetchval(
                "SELECT id FROM orders WHERE external_order_id = 'E-STABLE'")
            n_cust = await conn.fetchval(
                "SELECT COUNT(*) FROM customers"
                " WHERE member_id = 'M-900010'")
            n_ord = await conn.fetchval(
                "SELECT COUNT(*) FROM orders"
                " WHERE external_order_id = 'E-STABLE'")
            assert str(cid1) == str(cid2)  # join-back: same surrogate
            assert str(oid1) == str(oid2)
            assert int(n_cust) == 1 and int(n_ord) == 1
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_ld1_custom_data_merge_and_cascade(database_url: str) -> None:
    """LD1: side rows merge key-by-key on re-write and die with the entity."""
    import asyncpg

    from pipelines import apply as ap

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            cid = await conn.fetchval(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ('M-900020', 'Custom Carl')"
                " ON CONFLICT (member_id) DO UPDATE"
                " SET full_name = EXCLUDED.full_name RETURNING id")
            await ap.upsert_custom_data(conn, "customers", str(cid),
                                        {"nickname": "CC"})
            await ap.upsert_custom_data(conn, "customers", str(cid),
                                        {"tier": "gold", "nickname": "CJ"})
            data = await conn.fetchval(
                "SELECT data FROM customer_custom_data"
                " WHERE customer_id = $1", cid)
            import json
            assert json.loads(data) == {"nickname": "CJ", "tier": "gold"}
            await conn.execute(
                "DELETE FROM customers WHERE id = $1", cid)
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM customer_custom_data"
                " WHERE customer_id = $1", cid)
            assert int(n) == 0  # cascade
    finally:
        await pool.close()


def test_ld1_unknown_entity_rejected():
    from pipelines import apply as ap

    with pytest.raises(ValueError):
        ap.custom_data_upsert_sql("stores")


@pytest.mark.asyncio
async def test_apply_survives_a_find_that_missed(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """find-then-insert TOCTOU: the row lands between the check and the write.

    The pre-check is blinded (`FIND_ORDER_SQL` answers NULL) while the order
    is already there, so the INSERT is the only statement that can notice.
    Without ON CONFLICT the partial unique index raises UniqueViolationError
    and the whole job dies on one replayed row; with it the row is skipped
    and counted (review §4.3-4).
    """
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await _seed(conn)
            cid = await conn.fetchval(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ('M-900003', 'Race Buyer')"
                " ON CONFLICT (member_id) DO UPDATE"
                " SET full_name = EXCLUDED.full_name RETURNING id")
            sid = await conn.fetchval(
                "SELECT id FROM stores WHERE code = 'APPLY-S1'")
            # the concurrent writer's row, already committed
            await conn.execute(
                "INSERT INTO orders (customer_id, store_id, ordered_at,"
                " total_cents, status, source, external_order_id)"
                " VALUES ($1, $2, TIMESTAMPTZ '2026-09-02 10:00:00+00',"
                " 300, 'completed', 'pos', 'E-RACE-1')", cid, sid)
    finally:
        await pool.close()

    import pipelines.apply as ap

    monkeypatch.setattr(
        ap, "FIND_ORDER_SQL",
        "SELECT NULL::uuid WHERE $1::text IS NOT NULL"
        " AND $2::text IS NOT NULL")

    lines = json.dumps([{"sku": "APPLY-SKU", "qty": 3,
                         "unit_cents": 100}]).replace('"', '""')
    csv = ("store_id,external_order_id,customer_id,lines,ordered_at\n"
           f"APPLY-S1,E-RACE-1,M-900003,\"{lines}\",2026-09-02T10:00:00Z\n")
    out = await run_upload.run(
        _write(tmp_path, "race.csv", csv), "orders", "pos", None,
        None, None, apply=True)

    assert out["status"] == "committed", out
    assert out["skipped"] == 1 and out["apply_failed"] == []
    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE source = 'pos'"
                " AND external_order_id = 'E-RACE-1'")
            assert int(n) == 1, "the raced row must not be duplicated"
            total = await conn.fetchval(
                "SELECT total_cents FROM orders"
                " WHERE external_order_id = 'E-RACE-1'")
            assert int(total) == 300, "the winner's row must not be rewritten"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_reingest_revives_a_soft_deleted_customer(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """A rejoin has to bring the member back: leaving the tombstone in place
    made the customer invisible and quarantined every later order for them."""
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await _seed(conn)
            await conn.execute(
                "INSERT INTO customers (member_id, full_name, deleted_at)"
                " VALUES ('M-900010', 'Revived One', now())"
                " ON CONFLICT (member_id) DO UPDATE SET deleted_at = now()")
    finally:
        await pool.close()

    csv = "member_id,full_name\nM-900010,Revived One Again\n"
    out = await run_upload.run(
        _write(tmp_path, "revive.csv", csv), "customers", "pos", None,
        None, None, apply=True)
    assert out["status"] == "committed", out
    assert out["customers_revived"] == 1, out
    assert out["applied"] == 1

    lines = json.dumps([{"sku": "APPLY-SKU", "qty": 1,
                         "unit_cents": 100}]).replace('"', '""')
    csv_o = ("store_id,external_order_id,member_id,lines\n"
             f"APPLY-S1,E-REVIVE-1,M-900010,\"{lines}\"\n")
    out_o = await run_upload.run(
        _write(tmp_path, "revive_order.csv", csv_o), "orders", "pos",
        None, None, None, apply=True)
    assert out_o["status"] == "committed", out_o
    assert out_o["quarantined"] == 0 and out_o["applied"] == 1, out_o
    assert out_o["customers_revived"] == 0

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            live = await conn.fetchval(
                "SELECT deleted_at IS NULL FROM customers"
                " WHERE member_id = 'M-900010'")
            assert live is True, "the rejoin must clear the tombstone"
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM orders o JOIN customers c"
                " ON c.id = o.customer_id"
                " WHERE o.external_order_id = 'E-REVIVE-1'"
                "   AND c.member_id = 'M-900010'")
            assert int(n) == 1, "the revived member's order must apply"
            note = await conn.fetchval(
                "SELECT reason FROM staging_rows WHERE upload_job_id = $1"
                " ORDER BY row_number LIMIT 1", out["job_id"])
            assert note and "revived" in note and "M-900010" in note, note
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_unparseable_ordered_at_quarantines_with_the_value(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """`now()` filed a bad timestamp under today's revenue, where nothing
    downstream can tell it from a real one."""
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await _seed(conn)
            await conn.execute(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ('M-900011', 'Bad Timestamp')"
                " ON CONFLICT (member_id) DO NOTHING")
    finally:
        await pool.close()

    lines = json.dumps([{"sku": "APPLY-SKU", "qty": 1,
                         "unit_cents": 100}]).replace('"', '""')
    csv = ("store_id,external_order_id,member_id,lines,ordered_at\n"
           f"APPLY-S1,E-BADTS,M-900011,\"{lines}\",2026-02-31T10:00:00Z\n")
    out = await run_upload.run(
        _write(tmp_path, "badts.csv", csv), "orders", "pos", None,
        None, None, apply=True)
    assert out["apply_failed"] == [1], out
    assert out["quarantined"] == 1 and out["applied"] == 0, out

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            reason = await conn.fetchval(
                "SELECT reason FROM quarantine_rows WHERE upload_job_id = $1"
                " AND row_number = 1", out["job_id"])
            assert "2026-02-31T10:00:00Z" in reason, reason
            assert "now()" in reason, reason
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE source = 'pos'"
                " AND external_order_id = 'E-BADTS'")
            assert int(n) == 0, "a refused row must not be written at all"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_naive_ordered_at_reads_the_feed_zone_not_utc(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """The feed's local clock is +08; reading it as UTC moved an 01:30 order
    into the previous business day's bucket."""
    import asyncpg
    from datetime import datetime, timezone

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await _seed(conn)
            await conn.execute(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ('M-900012', 'Naive Clock')"
                " ON CONFLICT (member_id) DO NOTHING")
    finally:
        await pool.close()

    lines = json.dumps([{"sku": "APPLY-SKU", "qty": 1,
                         "unit_cents": 100}]).replace('"', '""')
    csv = ("store_id,external_order_id,member_id,lines,ordered_at\n"
           f"APPLY-S1,E-NAIVE-1,M-900012,\"{lines}\",2026-10-17T01:30:00\n")
    out = await run_upload.run(
        _write(tmp_path, "naivets.csv", csv), "orders", "pos", None,
        None, None, apply=True)
    assert out["status"] == "committed", out

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            when = await conn.fetchval(
                "SELECT ordered_at FROM orders WHERE source = 'pos'"
                " AND external_order_id = 'E-NAIVE-1'")
            assert when == datetime(2026, 10, 16, 17, 30, tzinfo=timezone.utc), \
                f"01:30 +08 is 17:30Z the day before, got {when}"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_completed_order_dated_in_the_future_is_quarantined(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """A completed sale cannot be dated after now.

    Measured on the live demo data (2026-10-20, manager's sweep): one order
    at 2027-03-01T20:00Z, `status='completed'`, put its customer above every
    real one on any `last_order_at` surface and never lapsed, because its
    recency is negative. The row is refused with its own code (not clamped to
    now(): a clamp files a fiction under today's revenue), and a future row
    that is *not* completed is still accepted -- a scheduled order is a
    different fact and refusing it would drop real data.
    """
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await _seed(conn)
            await conn.execute(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ('M-900013', 'Future Dated')"
                " ON CONFLICT (member_id) DO NOTHING")
    finally:
        await pool.close()

    lines = json.dumps([{"sku": "APPLY-SKU", "qty": 1,
                         "unit_cents": 100}]).replace('"', '""')
    # 2099 is far enough out to stay in the future for the life of the test.
    csv = ("store_id,external_order_id,member_id,lines,ordered_at,status\n"
           f"APPLY-S1,E-FUTURE,M-900013,\"{lines}\",2099-01-02T10:00:00Z,completed\n"
           f"APPLY-S1,E-SCHEDULED,M-900013,\"{lines}\",2099-01-03T10:00:00Z,pending\n")
    out = await run_upload.run(
        _write(tmp_path, "future.csv", csv), "orders", "pos", None,
        None, None, apply=True)
    assert out["quarantined"] == 1, out
    assert out["applied"] == 1, out
    assert out["quarantine_reasons"] == {"future_dated": 1}, out

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            code = await conn.fetchval(
                "SELECT code FROM quarantine_rows WHERE upload_job_id = $1"
                " AND row_number = 1", out["job_id"])
            assert code == "future_dated", code
            kept = await conn.fetch(
                "SELECT external_order_id, status FROM orders"
                " WHERE source = 'pos' AND external_order_id LIKE 'E-FUTURE%'"
                " OR source = 'pos' AND external_order_id = 'E-SCHEDULED'")
            assert [r["external_order_id"] for r in kept] == ["E-SCHEDULED"], \
                f"only the non-completed future row may be stored, got {kept}"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_run_summary_counts_quarantines_by_reason(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """A run that drops rows must say so, by reason, on its own summary.

    `quarantined_rows` alone is a number a reader cannot act on: the row is
    dropped and the run says "succeeded". The job carries `{code: count}`
    (migration 043), counted from the quarantine rows themselves.
    """
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await _seed(conn)
            await conn.execute(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ('M-910003', 'Reason Buyer')"
                " ON CONFLICT (member_id) DO NOTHING")
    finally:
        await pool.close()

    good = json.dumps([{"sku": "APPLY-SKU", "qty": 1,
                        "unit_cents": 100}]).replace('"', '""')
    nosku = json.dumps([{"sku": "NO-SUCH-SKU", "qty": 1,
                         "unit_cents": 100}]).replace('"', '""')
    csv = ("store_id,external_order_id,customer_id,lines,ordered_at\n"
           f"APPLY-S1,E-Q1,M-910003,\"{good}\",2026-09-01T10:00:00Z\n"
           f"APPLY-S1,E-Q2,M-910003,\"{good}\",not-a-date\n"
           f"APPLY-S1,E-Q3,M-910003,\"{nosku}\",2026-09-01T10:00:00Z\n")
    out = await run_upload.run(
        _write(tmp_path, "q.csv", csv), "orders", "pos", None,
        None, None, apply=True)

    assert out["status"] == "committed_with_rejects", out
    assert out["applied"] == 1
    assert out["quarantine_reasons"] == {"ordered_at_unparseable": 1,
                                         "sku_missing": 1}, out

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT quarantine_reasons FROM upload_jobs WHERE id = $1",
                out["job_id"])
            if isinstance(stored, str):
                stored = json.loads(stored)
            assert stored == out["quarantine_reasons"], stored
            codes = await conn.fetch(
                "SELECT code FROM quarantine_rows WHERE upload_job_id = $1"
                " ORDER BY code", out["job_id"])
            assert [r["code"] for r in codes] == ["ordered_at_unparseable",
                                                  "sku_missing"]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_quarantine_retry_after_the_feed_is_fixed(
        database_url: str, tmp_path: Path, monkeypatch) -> None:
    """Quarantine is not terminal: the fixed row applies and the reject retires.

    Append-only stays true -- the record is `resolved_at`, never edited or
    deleted -- and the second run is a clean one, so its summary is `{}`.
    """
    import asyncpg

    monkeypatch.setenv("DATABASE_URL", database_url)
    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await _seed(conn)
            await conn.execute(
                "INSERT INTO customers (member_id, full_name)"
                " VALUES ('M-910004', 'Retry Buyer')"
                " ON CONFLICT (member_id) DO NOTHING")
    finally:
        await pool.close()

    lines = json.dumps([{"sku": "APPLY-SKU", "qty": 3,
                         "unit_cents": 100}]).replace('"', '""')
    head = "store_id,external_order_id,customer_id,lines,ordered_at\n"
    bad = (head + f"APPLY-S1,E-RETRY-1,M-910004,\"{lines}\",not-a-date\n")
    out1 = await run_upload.run(
        _write(tmp_path, "r1.csv", bad), "orders", "pos", None,
        None, None, apply=True)
    assert out1["status"] == "committed_with_rejects", out1
    assert out1["quarantine_reasons"] == {"ordered_at_unparseable": 1}, out1

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            assert not await conn.fetchval(
                "SELECT COUNT(*) FROM orders"
                " WHERE external_order_id = 'E-RETRY-1'")
            open1 = await conn.fetchval(
                "SELECT COUNT(*) FROM quarantine_rows WHERE source = 'pos'"
                " AND key = 'E-RETRY-1' AND resolved_at IS NULL")
            assert int(open1) == 1
    finally:
        await pool.close()

    fixed = (head + f"APPLY-S1,E-RETRY-1,M-910004,\"{lines}\","
             "2026-09-02T10:00:00Z\n")
    out2 = await run_upload.run(
        _write(tmp_path, "r2.csv", fixed), "orders", "pos", None,
        None, None, apply=True)
    assert out2["status"] == "committed", out2
    assert out2["quarantine_reasons"] == {}, out2

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT total_cents FROM orders"
                " WHERE external_order_id = 'E-RETRY-1'")
            assert int(total) == 300
            open2 = await conn.fetchval(
                "SELECT COUNT(*) FROM quarantine_rows WHERE source = 'pos'"
                " AND key = 'E-RETRY-1' AND resolved_at IS NULL")
            assert int(open2) == 0
            retired = await conn.fetchval(
                "SELECT COUNT(*) FROM quarantine_rows WHERE source = 'pos'"
                " AND key = 'E-RETRY-1' AND resolved_at IS NOT NULL")
            assert int(retired) == 1
    finally:
        await pool.close()
