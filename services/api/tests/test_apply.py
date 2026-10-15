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
