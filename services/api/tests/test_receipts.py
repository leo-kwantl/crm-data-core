"""Landing-receipt tests (data LD12).

Pure: exactly-one-of job/vendor, count reconciliation, field shape.
DB: receipt issues against a real upload_job, dup job rejected
(UNIQUE), vendor-batch receipt without a job, manifest FK enforced.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from pipelines.receipts import validate_receipt

pytestmark = pytest.mark.asyncio


def test_validate_ok_upload_job():
    job = uuid4()
    out = validate_receipt({"upload_job_id": str(job), "source": "upload",
                            "rows_seen": 10, "rows_loaded": 9,
                            "rows_quarantined": 1, "contract": "orders"})
    assert out["upload_job_id"] == job
    assert out["vendor_batch_id"] is None
    assert out["manifest_pipeline"] is None


def test_validate_ok_vendor_batch():
    out = validate_receipt({"vendor_batch_id": "V-2026-001",
                            "source": "acme-sftp", "rows_seen": 50,
                            "rows_loaded": 50, "contract": "events",
                            "manifest_pipeline": "nightly"})
    assert out["upload_job_id"] is None
    assert out["manifest_pipeline"] == "nightly"


def test_validate_rejects_both_or_neither():
    base = {"source": "s", "rows_seen": 1, "rows_loaded": 1,
            "contract": "orders"}
    with pytest.raises(ValueError, match="exactly one"):
        validate_receipt({**base, "upload_job_id": str(uuid4()),
                          "vendor_batch_id": "V-1"})
    with pytest.raises(ValueError, match="exactly one"):
        validate_receipt(dict(base))


def test_validate_rejects_unreconciled_counts():
    with pytest.raises(ValueError, match="exceeds seen"):
        validate_receipt({"vendor_batch_id": "V-1", "source": "s",
                          "rows_seen": 5, "rows_loaded": 4,
                          "rows_quarantined": 2, "contract": "orders"})


async def _migrate_all(conn) -> None:
    import re
    from pathlib import Path

    up_re = re.compile(
        r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)", re.S | re.I)
    mig_dir = (Path(__file__).resolve().parents[3] / "db" / "migrations")
    for mig in sorted(mig_dir.glob("*.sql")):
        m = up_re.search(mig.read_text(encoding="utf-8"))
        if m and m.group(1).strip():
            try:
                await conn.execute(m.group(1).strip())
            except Exception as exc:
                if "already exists" not in str(exc).lower():
                    raise


async def test_receipt_lifecycle_and_manifest_link(database_url: str):
    """Upload-job receipt issues once; vendor receipt needs no job;
    manifest link enforced by FK; receipts immutable (no UPDATE)."""
    import asyncpg

    from pipelines.receipts import RECEIPT_INSERT_SQL

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            if await conn.fetchval(
                    "SELECT to_regclass('public.landing_receipts')") is None:
                await _migrate_all(conn)
            job = await conn.fetchval(
                "INSERT INTO upload_jobs (filename, file_hash, kind)"
                " VALUES ('ld12.csv', 'ld12hash', 'orders')"
                " RETURNING id")
            await conn.execute(
                "INSERT INTO pipeline_manifests (pipeline, yaml)"
                " VALUES ('ld12-probe', 'pipelines: {}')"
                " ON CONFLICT (pipeline) DO NOTHING")
            spec = validate_receipt(
                {"upload_job_id": str(job), "source": "upload",
                 "rows_seen": 10, "rows_loaded": 10,
                 "contract": "orders",
                 "manifest_pipeline": "ld12-probe"})
            row = await conn.fetchrow(
                RECEIPT_INSERT_SQL, spec["upload_job_id"],
                spec["vendor_batch_id"], spec["source"],
                spec["rows_seen"], spec["rows_loaded"],
                spec["rows_quarantined"], spec["contract"],
                spec["manifest_pipeline"])
            assert row["id"] is not None
            # One receipt per job: second insert hits the UNIQUE.
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.fetchrow(
                    RECEIPT_INSERT_SQL, spec["upload_job_id"], None,
                    "upload", 1, 1, 0, "orders", None)
            # Vendor batch: no job, bad manifest hits the FK.
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.fetchrow(
                    RECEIPT_INSERT_SQL, None, "V-LD12", "sftp",
                    5, 5, 0, "events", "no-such-pipeline")
            vrow = await conn.fetchrow(
                RECEIPT_INSERT_SQL, None, "V-LD12", "sftp",
                5, 4, 1, "events", None)
            assert vrow["id"] is not None
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM landing_receipts"
                " WHERE vendor_batch_id = 'V-LD12'"
                " OR upload_job_id = $1", job)
            assert int(n) == 2
            await conn.execute(
                "DELETE FROM landing_receipts"
                " WHERE vendor_batch_id = 'V-LD12'")
            await conn.execute(
                "DELETE FROM landing_receipts WHERE upload_job_id = $1",
                job)
            await conn.execute(
                "DELETE FROM upload_jobs WHERE id = $1", job)
            # The probe row is not a schedule. Left behind it breaks
            # test_schedule.py::test_live_manifest_rows_are_reachable_by_the_tick,
            # which requires every stored manifest to declare one (it skips
            # only when pipeline_manifests is empty).
            await conn.execute(
                "DELETE FROM pipeline_manifests"
                " WHERE pipeline = 'ld12-probe'")
    finally:
        await pool.close()
