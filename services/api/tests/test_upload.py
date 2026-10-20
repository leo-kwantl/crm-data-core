"""Batch upload ETL tests (data owns upload ETL).

Pure unit: hash determinism, CSV shape errors, per-kind validate split,
reconciliation invariant. DB: same file twice -> one job (no double stage),
quarantine rows persist with reason + raw.
"""
from __future__ import annotations

import json

import pytest

from pipelines import upload as up

ORDERS_CSV = (
    "store_id,external_order_id,customer_id,lines\n"
    '11111111-1111-1111-1111-111111111111,POS-1,,'
    '"[{ ""sku"": ""A"", ""qty"": 2, ""unit_cents"": 500}]"\n'
    ',POS-2,,'
    '"[{ ""sku"": ""B"", ""qty"": 1, ""unit_cents"": 100}]"\n'
)

CUSTOMERS_CSV = (
    "member_id,full_name,external_member_id\n"
    "M-000001,Alice,POS-1001\n"
    "BAD-1,Bob,\n"
)


def test_file_hash_deterministic():
    assert up.file_hash(b"abc") == up.file_hash(b"abc")
    assert up.file_hash(b"abc") != up.file_hash(b"abd")


def test_three_decimal_unit_price_is_exact_not_float():
    """`float("2.675") * 100` is 267.49999999999997, which rounded to 267 --
    a cent lost on a price that is exactly 267.5 cents (half up -> 268)."""
    got = up.normalize_row(
        {"store_id": "S1", "external_order_id": "E-MONEY-1",
         "lines": json.dumps([{"sku": "SKU-1", "qty": 1,
                               "unit": 2.675}])}, "orders")
    assert got["total_cents"] == 268


def test_fractional_qty_is_refused_not_truncated():
    """`int(float("2.5"))` was 2: a line silently lost half the goods."""
    with pytest.raises(ValueError, match="qty: must be a whole number"):
        up.normalize_row(
            {"store_id": "S1", "external_order_id": "E-MONEY-2",
             "lines": json.dumps([{"sku": "SKU-1", "qty": 2.5,
                                   "unit_cents": 100}])}, "orders")


def test_feed_total_must_match_the_line_sum():
    """A discount/shipping/tax is exactly this shape and the row has to name
    both numbers instead of quietly picking one of them."""
    with pytest.raises(ValueError, match="1000 does not match the line sum"):
        up.normalize_row(
            {"store_id": "S1", "external_order_id": "E-MONEY-3",
             "total_cents": "1000",
             "lines": json.dumps([{"sku": "SKU-1", "qty": 3,
                                   "unit_cents": 400}])}, "orders")


def test_present_feed_total_is_what_gets_persisted():
    """When the feed agrees, its own number is the one stored (it is the
    audited figure); with no feed total the line sum is kept, not invented."""
    got = up.normalize_row(
        {"store_id": "S1", "external_order_id": "E-MONEY-4",
         "total_cents": "1000",
         "lines": json.dumps([{"sku": "SKU-1", "qty": 2,
                               "unit_cents": 500}])}, "orders")
    assert got["total_cents"] == 1000
    no_total = up.normalize_row(
        {"store_id": "S1", "external_order_id": "E-MONEY-5",
         "lines": json.dumps([{"sku": "SKU-1", "qty": 2,
                               "unit_cents": 500}])}, "orders")
    assert no_total["total_cents"] == 1000


def test_parse_csv_missing_columns():
    with pytest.raises(ValueError, match="missing columns"):
        up.parse_csv("member_id\nM-000001\n", "customers")


def test_parse_csv_empty():
    with pytest.raises(ValueError, match="empty file"):
        up.parse_csv("member_id,full_name\n", "customers")


def test_validate_orders_split():
    rows = up.parse_csv(ORDERS_CSV, "orders")
    ok, bad = up.validate_batch(rows, "orders")
    assert len(ok) == 1 and len(bad) == 1
    assert ok[0]["normalized"]["total_cents"] == 1000
    assert "store_id" in bad[0]["reason"]


def test_validate_customers_split():
    rows = up.parse_csv(CUSTOMERS_CSV, "customers")
    ok, bad = up.validate_batch(rows, "customers")
    assert [o["normalized"]["member_id"] for o in ok] == ["M-000001"]
    assert bad[0]["key"] == "BAD-1"


def test_decode_plain_and_gzip():
    import gzip as _gzip

    text, enc = up.decode_upload_bytes(CUSTOMERS_CSV.encode())
    assert enc == "identity" and "M-000001" in text
    gz = _gzip.compress(CUSTOMERS_CSV.encode())
    big = (CUSTOMERS_CSV.encode()) * 50  # realistic size compresses well
    assert len(_gzip.compress(big)) < len(big)
    text2, enc2 = up.decode_upload_bytes(gz)
    assert enc2 == "gzip" and text2 == text
    rows = up.parse_csv(text2, "customers")  # gzipped content validates
    ok, bad = up.validate_batch(rows, "customers")
    assert len(ok) == 1 and len(bad) == 1


def test_decode_caps(monkeypatch):
    import gzip as _gzip

    monkeypatch.setattr(up, "MAX_DECOMPRESSED_BYTES", 10)
    with pytest.raises(ValueError, match="decompressed"):
        up.decode_upload_bytes(_gzip.compress(b"x" * 100))
    monkeypatch.setattr(up, "MAX_RAW_BYTES", 4)
    with pytest.raises(ValueError, match="too large"):
        up.decode_upload_bytes(b"12345")


def test_reconcile_drift_raises():
    with pytest.raises(ValueError, match="drift"):
        up.reconcile(3, 1, 1)
    up.reconcile(2, 1, 1)  # no raise


def test_identity_and_events_kinds():
    rows = up.parse_csv(
        "member_id,external_member_id,source\nM-000002,POS-9,pos\n", "identity")
    ok, bad = up.validate_batch(rows, "identity")
    assert len(ok) == 1 and not bad
    rows = up.parse_csv(
        "customer_id,type,source,source_event_id\n"
        "22222222-2222-2222-2222-222222222222,note,pos,E-1\n", "events")
    ok, bad = up.validate_batch(rows, "events")
    assert len(ok) == 1 and not bad


@pytest.mark.asyncio
async def test_upload_idempotent_and_quarantine(database_url: str):
    """Same customers CSV twice -> one job; 1 ok + 1 quarantined.

    Purges its own job first and last: the dedup key is `(filename, file_hash)`
    with fixed CSV constants, so a job left by an earlier run makes
    `CREATE_JOB_SQL` answer NULL and this test fails on a *reused* job that
    never read the file -- measured 2026-10-20 as one of the full-suite
    modules that wrote outside a namespace it cleaned.
    """
    import re
    from pathlib import Path

    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT to_regclass('public.upload_jobs')")
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
            digest = up.file_hash(CUSTOMERS_CSV.encode())
            rows = up.parse_csv(CUSTOMERS_CSV, "customers")

            async def _purge() -> int:
                """Rows this module owns, matched by the key that dedups them."""
                tag = await conn.execute(
                    "DELETE FROM upload_jobs"
                    " WHERE filename = $1 AND file_hash = $2",
                    "members.csv", digest)
                return int(tag.split()[-1])

            await _purge()
            job_id = await conn.fetchval(
                up.CREATE_JOB_SQL, "members.csv", digest, "customers",
                "v1", "upload", len(rows), None, None, None)
            assert job_id is not None
            dup = await conn.fetchval(
                up.CREATE_JOB_SQL, "members.csv", digest, "customers",
                "v1", "upload", len(rows), None, None, None)
            assert dup is None  # conflict -> no second job
            for r in rows:
                await conn.execute(up.STAGE_ROWS_SQL, job_id,
                                   r["row_number"], json.dumps(r["raw"]))
            ok, bad = up.validate_batch(rows, "customers")
            for b in bad:
                await conn.execute(up.WRITE_QUARANTINE_SQL, job_id,
                                   b["row_number"], "upload", b["key"],
                                   b["reason"], json.dumps(b["raw"]),
                                   b["code"])
            await conn.execute(up.FINISH_JOB_SQL, job_id,
                               "committed_with_rejects",
                               len(rows), len(rows), len(ok), len(bad), None,
                               "{}")
            q = await conn.fetch(
                "SELECT row_number, key, reason, raw FROM quarantine_rows"
                " WHERE upload_job_id = $1", job_id)
            assert len(q) == 1 and q[0]["key"] == "BAD-1"
            assert "member_id" in q[0]["reason"]
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM upload_jobs WHERE file_hash = $1",
                digest)
            assert int(n) == 1
            await _purge()
            left = await conn.fetchval(
                "SELECT COUNT(*) FROM upload_jobs WHERE file_hash = $1",
                digest)
            assert int(left) == 0, "left its job behind; the next run reads a"
            " reused job and applies nothing"
    finally:
        await pool.close()
