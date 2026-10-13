"""Core-table apply for staged upload rows (batch-upload ETL phase 3).

Stage+validate never touch core tables. This module applies `ok` staged
rows into `customers` / `orders`(+`order_items`) / `customer_events` /
identity bindings, idempotently, one transaction per call:

- customers: upsert on `member_id` (second-see updates name/external id);
  contacts appended with ON CONFLICT DO NOTHING.
- orders: resolve store (id-or-code) + customer (uuid/member/external) +
  product SKUs; pre-check `(source, external_order_id)` so re-apply =
  no-op skip. Optional raw `ordered_at` (ISO) honoured, else now().
  Missing store/customer/SKU -> apply-fail (quarantine, not crash).
- events: pre-check `(source, source_event_id)` -> no-op skip; customer
  resolved the same way as orders.
- identity: binds external id onto the existing member row; unknown
  member -> apply-fail (quarantine).

Reconciliation: committed = applied + dedup-skipped; quarantined grows
by apply-fails, so total == committed + quarantined still holds.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from pipelines import upload as up

RESOLVE_STORE_SQL = """
SELECT id FROM stores WHERE code = $1 OR id::text = $1
"""

RESOLVE_CUSTOMER_SQL = """
SELECT id FROM customers
WHERE deleted_at IS NULL
  AND (id::text = $1 OR member_id = $1 OR external_member_id = $1)
"""

RESOLVE_PRODUCT_SQL = "SELECT id FROM products WHERE sku = $1"

CUSTOMER_UPSERT_SQL = """
INSERT INTO customers (member_id, full_name, external_member_id)
VALUES ($1, $2, $3)
ON CONFLICT (member_id) DO UPDATE SET
  full_name = EXCLUDED.full_name,
  external_member_id = COALESCE(EXCLUDED.external_member_id,
                                customers.external_member_id),
  updated_at = now()
RETURNING id
"""

CONTACT_INSERT_SQL = """
INSERT INTO customer_contacts
  (customer_id, kind, value_normalized, value_display)
VALUES ($1, $2, $3, $4)
ON CONFLICT (kind, value_normalized) DO NOTHING
"""

IDENTITY_BIND_SQL = """
UPDATE customers SET external_member_id = $2, updated_at = now()
WHERE member_id = $1 AND deleted_at IS NULL
"""

FIND_ORDER_SQL = """
SELECT id FROM orders WHERE source = $1 AND external_order_id = $2
"""

INSERT_ORDER_SQL = """
INSERT INTO orders
  (customer_id, store_id, ordered_at, total_cents, status,
   source, external_order_id)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING id
"""

INSERT_LINE_SQL = """
INSERT INTO order_items (order_id, product_id, qty, unit_price_cents)
VALUES ($1, $2, $3, $4)
"""

FIND_EVENT_SQL = """
SELECT id FROM customer_events WHERE source = $1 AND source_event_id = $2
"""

INSERT_EVENT_SQL = """
INSERT INTO customer_events
  (customer_id, kind, source, source_event_id, summary, payload,
   occurred_at)
VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
RETURNING id
"""

STAGED_OK_SQL = """
SELECT row_number, raw FROM staging_rows
WHERE upload_job_id = $1 AND status = 'ok' ORDER BY row_number
"""

QUARANTINE_INSERT_SQL = """
INSERT INTO quarantine_rows (upload_job_id, row_number, source, key,
                             reason, raw)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
ON CONFLICT (upload_job_id, row_number) DO NOTHING
"""

MARK_STAGED_SQL = """
UPDATE staging_rows SET status = $3, reason = $4
WHERE upload_job_id = $1 AND row_number = $2
"""


async def resolve_store(conn, ref: str) -> Optional[str]:
    if not ref:
        return None
    return await conn.fetchval(RESOLVE_STORE_SQL, ref)


async def resolve_customer(conn, *refs: Any) -> Optional[str]:
    for ref in refs:
        if ref:
            hit = await conn.fetchval(RESOLVE_CUSTOMER_SQL, str(ref))
            if hit:
                return str(hit)
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ordered_at(raw: dict) -> datetime:
    val = (raw.get("ordered_at") or "").strip() if raw.get("ordered_at") else ""
    if val:
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return _now()


async def _apply_customer(conn, norm: dict, raw: dict) -> str:
    cid = await conn.fetchval(
        CUSTOMER_UPSERT_SQL, norm["member_id"], norm["full_name"],
        norm.get("external_member_id"))
    cid = str(cid)
    for kind in ("phone", "email"):
        val = (raw.get(kind) or "").strip()
        if not val:
            continue
        normed = val.lower() if kind == "email" else val
        await conn.execute(CONTACT_INSERT_SQL, cid, kind, normed, val)
    return cid


async def _apply_identity(conn, norm: dict, raw: dict) -> str:
    tag = await conn.execute(
        IDENTITY_BIND_SQL, norm["member_id"], norm["external_member_id"])
    if tag.split()[-1] == "0":
        raise LookupError(f"unknown member_id {norm['member_id']}")
    cid = await resolve_customer(conn, norm["member_id"])
    assert cid is not None
    return cid


async def _apply_order(conn, norm: dict, raw: dict,
                       source: str) -> Optional[str]:
    ext = norm["external_order_id"]
    if await conn.fetchval(FIND_ORDER_SQL, source, ext):
        return None  # replay: already applied
    store_id = await resolve_store(conn, norm["store_id"])
    if store_id is None:
        raise LookupError(f"unknown store {norm['store_id']!r}")
    cid = await resolve_customer(
        conn, raw.get("customer_id"), raw.get("member_id"),
        raw.get("external_member_id"))
    if cid is None:
        raise LookupError("unresolvable customer (need customer_id, "
                          "member_id or external_member_id)")
    product_ids: list[tuple[str, int, int]] = []
    for line in norm["lines"]:
        pid = await conn.fetchval(RESOLVE_PRODUCT_SQL, line["sku"])
        if pid is None:
            raise LookupError(f"unknown sku {line['sku']!r}")
        product_ids.append((str(pid), line["qty"], line["unit_cents"]))
    oid = await conn.fetchval(
        INSERT_ORDER_SQL, cid, store_id, _ordered_at(raw),
        norm["total_cents"], (raw.get("status") or "completed")[:20],
        source, ext)
    oid = str(oid)
    for pid, qty, cents in product_ids:
        await conn.execute(INSERT_LINE_SQL, oid, pid, qty, cents)
    await _write_order_placed(conn, cid, store_id, oid,
                              _ordered_at(raw), norm["total_cents"], ext)
    return oid


DERIVED_EVENT_SOURCE = "derived"
DERIVED_ORDER_KIND = "order_placed"  # name-keyed in ml calibration SQL;
# renaming requires ml ack FIRST (see contracts.md kind vocabulary).


async def _write_order_placed(conn, cid: str, store_id: str, oid: str,
                              ordered_at, total_cents: int,
                              ext: Optional[str]) -> None:
    evid = f"order:{oid}"
    if await conn.fetchval(FIND_EVENT_SQL, DERIVED_EVENT_SOURCE, evid):
        return  # replay: derived event already written
    label = ext or oid[:8]
    summary = f"Order {label} \u00b7 ${total_cents / 100:,.2f}"[:200]
    payload = json.dumps({"order_id": oid, "total_cents": total_cents,
                          "store_id": str(store_id)})
    await conn.execute(INSERT_EVENT_SQL, cid, DERIVED_ORDER_KIND,
                       DERIVED_EVENT_SOURCE, evid, summary, payload,
                       ordered_at)


async def _apply_event(conn, norm: dict, raw: dict,
                       source: str) -> Optional[str]:
    if await conn.fetchval(FIND_EVENT_SQL, source,
                           norm["source_event_id"]):
        return None  # replay: already applied
    cid = await resolve_customer(
        conn, norm.get("customer_id"), raw.get("member_id"),
        raw.get("external_member_id"))
    if cid is None:
        raise LookupError("unresolvable customer (need customer_id, "
                          "member_id or external_member_id)")
    occurred = norm.get("occurred_at") or _now()
    if isinstance(occurred, str):
        occurred = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
    summary = (raw.get("summary") or f"{norm['type']} event")[:200]
    await conn.execute(
        INSERT_EVENT_SQL, cid, norm["type"], source,
        norm["source_event_id"], summary,
        json.dumps(norm.get("payload") or {}), occurred)
    return "event"


_APPLIERS = {
    "customers": _apply_customer,
    "orders": _apply_order,
    "events": _apply_event,
    "identity": _apply_identity,
}


async def apply_job(conn, job_id: str, kind: str,
                    source: str) -> dict[str, Any]:
    """Apply all `ok` staged rows of a job to core tables.

    Runs inside the caller's transaction. Apply-fails are written to
    `quarantine_rows` (and staged rows marked `rejected`) so the caller
    can reconcile: committed = applied + skipped,
    quarantined += failed.
    """
    if kind not in _APPLIERS:
        raise ValueError(f"unknown kind {kind!r}")
    applier = _APPLIERS[kind]
    applied = 0
    skipped = 0
    failed: list[dict[str, Any]] = []
    rows = await conn.fetch(STAGED_OK_SQL, job_id)
    for rec in rows:
        row_no = rec["row_number"]
        raw = rec["raw"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        try:
            norm = up.normalize_row(raw, kind)
        except ValueError as exc:
            reason = f"revalidate failed: {exc}"
            failed.append({"row_number": row_no, "reason": reason})
            await conn.execute(QUARANTINE_INSERT_SQL, job_id, row_no,
                               source, up.row_key(raw, kind), reason,
                               json.dumps(raw))
            await conn.execute(MARK_STAGED_SQL, job_id, row_no,
                               "rejected", reason)
            continue
        try:
            if kind in ("orders", "events"):
                result = await applier(conn, norm, raw, source)
            else:
                result = await applier(conn, norm, raw)
        except (LookupError, ValueError) as exc:
            reason = f"apply failed: {exc}"
            failed.append({"row_number": row_no, "reason": reason})
            await conn.execute(QUARANTINE_INSERT_SQL, job_id, row_no,
                               source, up.row_key(raw, kind), reason,
                               json.dumps(raw))
            await conn.execute(MARK_STAGED_SQL, job_id, row_no,
                               "rejected", reason)
            continue
        if result is None:
            skipped += 1
        else:
            applied += 1
        # success: row stays 'ok' (no new status needed — the CHECK enum
        # is ('pending','ok','rejected')); replay safety comes from the
        # per-kind dedup pre-checks (orders/events skip) and convergent
        # upserts (customers/identity), not from row state.
    return {"applied": applied, "skipped": skipped, "failed": failed}
