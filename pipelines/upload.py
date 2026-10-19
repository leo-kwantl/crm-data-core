"""Batch upload ETL: file bytes -> staged rows -> validated -> quarantine.

Pipeline stages (data owns all of them; app only posts the file edge):
  receive (hash + job row, idempotent on file_hash x kind x scope)
  -> stage (raw lines into staging_rows, status=pending)
  -> validate (pipelines/ingest normalize per kind; ok/rejected)
  -> commit (quarantine rejects persist; job counts + status update)

Kinds v1: customers / orders / events / identity. CSV only (XLSX later).
Money arrives as decimal units (e.g. "12.50") or integer cents in
`<field>_cents`; lines for orders ride as a JSON list in the `lines`
column. Reconciliation invariant: total == committed + quarantined.

Transport encoding: front→back files SHOULD be gzip (`.gz` upload or
`Content-Encoding: gzip`); the backend sniffs the gzip magic (not the
extension) via `decode_upload_bytes()` and caps raw at 10MB /
decompressed at 64MB (zip-bomb guard). `file_hash` stays over the raw
transmitted bytes, so dedup is encoding-independent.
"""
from __future__ import annotations

from pipelines.ingest.errors import code_of

import csv
import gzip
import hashlib
import io
import json
import re
from typing import Any

from pipelines.ingest import events as events_ingest
from pipelines.ingest import identity as identity_ingest
from pipelines.ingest import orders as orders_ingest

KINDS = ("customers", "orders", "events", "identity")

REQUIRED_COLUMNS: dict[str, list[str]] = {
    "customers": ["member_id", "full_name"],
    "orders": ["store_id", "external_order_id", "lines"],
    "events": ["customer_id", "type", "source", "source_event_id"],
    "identity": ["member_id", "external_member_id", "source"],
}

MEMBER_RE = re.compile(r"^M-[0-9]{6}$")

GZIP_MAGIC = b"\x1f\x8b"
MAX_RAW_BYTES = 10 * 1024 * 1024  # transmitted file cap (10MB v1)
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024  # zip-bomb guard


def decode_upload_bytes(data: bytes) -> tuple[str, str]:
    """Raw received bytes -> (csv_text, encoding).

    Sniffs the gzip magic (extension-agnostic: works for `.gz` files and
    `Content-Encoding: gzip` bodies alike). `file_hash` stays over the raw
    transmitted bytes, so re-upload dedup is unaffected by encoding.
    Raises ValueError on over-limit raw or decompressed payloads.
    """
    if len(data) > MAX_RAW_BYTES:
        raise ValueError(
            f"file too large: {len(data)} bytes "
            f"(limit {MAX_RAW_BYTES})")
    if data[:2] == GZIP_MAGIC:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as fh:
            raw = fh.read(MAX_DECOMPRESSED_BYTES + 1)
        if len(raw) > MAX_DECOMPRESSED_BYTES:
            raise ValueError(
                f"decompressed file too large (limit "
                f"{MAX_DECOMPRESSED_BYTES} bytes)")
        try:
            return raw.decode("utf-8-sig"), "gzip"
        except UnicodeDecodeError as exc:
            raise ValueError(f"decompressed bytes are not UTF-8: {exc}") from exc
    try:
        return data.decode("utf-8-sig"), "identity"
    except UnicodeDecodeError as exc:
        raise ValueError(f"file bytes are not UTF-8: {exc}") from exc


def file_hash(data: bytes) -> str:
    """SHA-256 hex of raw file bytes (upload_jobs.file_hash, re-upload key)."""
    return hashlib.sha256(data).hexdigest()


def parse_csv(text: str, kind: str) -> list[dict]:
    """Parse CSV text -> [{row_number, raw}]. Raises ValueError on bad shape."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("empty file (no header row)")
    missing = [c for c in REQUIRED_COLUMNS[kind] if c not in reader.fieldnames]
    if missing:
        raise ValueError(f"missing columns for {kind}: {missing}")
    rows = []
    for i, record in enumerate(reader, start=1):
        if all((v is None or str(v).strip() == "") for v in record.values()):
            continue  # skip blank lines
        rows.append({"row_number": i,
                     "raw": {k: (v.strip() if isinstance(v, str) else v)
                             for k, v in record.items()}})
    if not rows:
        raise ValueError("empty file (no data rows)")
    return rows


def _coerce_lines(raw_lines: Any) -> list[dict]:
    """Accept a JSON list string or an already-parsed list; money stays exact."""
    items = json.loads(raw_lines) if isinstance(raw_lines, str) else raw_lines
    if not isinstance(items, list):
        raise ValueError(f"lines must be a JSON list, got {raw_lines!r}")
    out = []
    for ln in items:
        if not isinstance(ln, dict):
            raise ValueError(f"line must be an object, got {ln!r}")
        # Parsed through `Decimal` in the order contract's own helpers: a
        # float here loses a cent on values like "2.675", and `int(float(qty))`
        # truncates a fractional count instead of refusing it.
        qty = orders_ingest.whole_number(ln.get("qty", 0), field="qty")
        if "unit_cents" in ln:
            unit = orders_ingest.money_cents(ln["unit_cents"],
                                             field="unit_cents")
        else:
            unit = orders_ingest.money_cents(ln.get("unit", 0), field="unit",
                                             scale=100)
        out.append({"sku": ln.get("sku"), "qty": qty, "unit_cents": unit})
    return out


def normalize_customer(raw: dict) -> dict:
    """Validate one customer upload row. Raises ValueError."""
    member_id = (raw.get("member_id") or "").strip()
    if not MEMBER_RE.match(member_id):
        raise ValueError(f"bad member_id (want M-NNNNNN): {member_id!r}")
    full_name = (raw.get("full_name") or "").strip()
    if not 1 <= len(full_name) <= 200:
        raise ValueError("full_name must be 1..200 chars")
    out: dict[str, Any] = {"member_id": member_id, "full_name": full_name}
    if raw.get("external_member_id"):
        out["external_member_id"] = raw["external_member_id"].strip()
    if raw.get("source"):
        out["source"] = raw["source"].strip()
    for contact_kind in ("phone", "email"):
        if raw.get(contact_kind):
            out[contact_kind] = raw[contact_kind].strip()
    return out


def normalize_row(raw: dict, kind: str) -> dict:
    """Dispatch one raw row to its ingest normalizer. Raises ValueError."""
    if kind == "customers":
        return normalize_customer(raw)
    if kind == "orders":
        adapted = dict(raw)
        adapted["lines"] = _coerce_lines(raw.get("lines"))
        return orders_ingest.normalize(adapted)
    if kind == "events":
        return events_ingest.normalize(raw)
    if kind == "identity":
        return identity_ingest.normalize_binding(raw)
    raise ValueError(f"unknown kind: {kind}")


def row_key(normalized: dict, kind: str) -> str | None:
    """Best-effort dedup key for quarantine (source, key) display."""
    if kind == "orders":
        return normalized.get("external_order_id")
    if kind == "events":
        return normalized.get("source_event_id")
    return normalized.get("external_member_id") or normalized.get("member_id")


def validate_batch(rows: list[dict], kind: str) -> tuple[list[dict], list[dict]]:
    """Split staged rows -> (ok [{row_number, normalized}], bad [quarantine])."""
    ok, bad = [], []
    for row in rows:
        try:
            normalized = normalize_row(row["raw"], kind)
            ok.append({"row_number": row["row_number"], "normalized": normalized})
        except (ValueError, TypeError, KeyError) as exc:
            bad.append({"row_number": row["row_number"],
                        "key": row["raw"].get("external_order_id")
                        or row["raw"].get("source_event_id")
                        or row["raw"].get("external_member_id")
                        or row["raw"].get("member_id"),
                        "reason": str(exc)[:500],
                        "code": code_of(exc),
                        "raw": row["raw"]})
    return ok, bad


def reconcile(total: int, committed: int, quarantined: int) -> None:
    """Enforce total == committed + quarantined. Raises ValueError on drift."""
    if total != committed + quarantined:
        raise ValueError(
            f"reconciliation drift: total={total} != "
            f"committed={committed} + quarantined={quarantined}")


# --- Job SQL (parameterised; runner binds values, never formats) ---

FIND_JOB_SQL = """
SELECT id, status, total_rows, committed_rows, quarantined_rows
FROM upload_jobs
WHERE file_hash = $1 AND kind = $2
  AND brand_id IS NOT DISTINCT FROM $3::uuid
  AND group_id IS NOT DISTINCT FROM $4::uuid
"""

CREATE_JOB_SQL = """
INSERT INTO upload_jobs
  (filename, file_hash, kind, schema_version, source, status, total_rows,
   created_by, brand_id, group_id)
VALUES ($1, $2, $3, $4, $5, 'received', $6, $7, $8::uuid, $9::uuid)
ON CONFLICT DO NOTHING
RETURNING id
"""

STAGE_ROWS_SQL = """
INSERT INTO staging_rows (upload_job_id, row_number, raw)
VALUES ($1, $2, $3::jsonb)
ON CONFLICT (upload_job_id, row_number) DO NOTHING
"""

MARK_ROWS_SQL = """
UPDATE staging_rows SET status = $3, reason = $4
WHERE upload_job_id = $1 AND row_number = $2
"""

WRITE_QUARANTINE_SQL = """
INSERT INTO quarantine_rows (upload_job_id, row_number, source, key, reason,
                             raw, code)
VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
ON CONFLICT (upload_job_id, row_number) DO NOTHING
"""

REASON_COUNTS_SQL = """
SELECT COALESCE(code, 'other') AS code, COUNT(*) AS n
FROM quarantine_rows WHERE upload_job_id = $1 GROUP BY 1 ORDER BY 1
"""


async def reason_counts(conn, job_id) -> dict[str, int]:
    """`{reason_code: rows}` for one job, read back from the quarantine rows.

    Counted from the table rather than accumulated in Python so a job swept
    twice (validate, then apply) still reports every quarantined row once.
    """
    return {r["code"]: int(r["n"])
            for r in await conn.fetch(REASON_COUNTS_SQL, job_id)}


FINISH_JOB_SQL = """
UPDATE upload_jobs
SET status = $2, total_rows = $3, staged_rows = $4,
    committed_rows = $5, quarantined_rows = $6, error = $7,
    quarantine_reasons = $8::jsonb
WHERE id = $1
"""
