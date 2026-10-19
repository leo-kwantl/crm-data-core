"""Core-table apply tests: staged ok rows -> core tables, idempotently.

End-to-end through scripts/run_upload.run(..., apply=True) against the
session pgembed DB (unique keys per test; shared DB across files).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "run_upload", REPO / "scripts" / "run_upload.py")
assert _spec is not None and _spec.loader is not None
run_upload = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_upload)


async def _seed(conn) -> None:  # type: ignore[no-untyped-def]
    await conn.execute(
        "INSERT INTO stores (code, name, brand_id)"
        " VALUES ('APPLY-S1', 'Apply Store',"
        " '60100000-0000-0000-0000-000000000001')"
        " ON CONFLICT (code) DO NOTHING")
    await conn.execute(
        "INSERT INTO products (sku, name, category, price_cents)"
        " VALUES ('APPLY-SKU', 'Apply Widget', 'test', 100)"
        " ON CONFLICT (sku) DO NOTHING")


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


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
                "SELECT COUNT(*) FROM customer_events"
                " WHERE kind = 'order_placed' AND source = 'derived'")
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
