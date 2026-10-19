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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pipelines import upload as up
from pipelines.ingest.errors import RowMissing, RowRejected, code_of

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
WITH prev AS (
  SELECT id, deleted_at FROM customers WHERE member_id = $1
), upserted AS (
  INSERT INTO customers (member_id, full_name, external_member_id)
  VALUES ($1, $2, $3)
  ON CONFLICT (member_id) DO UPDATE SET
    full_name = EXCLUDED.full_name,
    external_member_id = COALESCE(EXCLUDED.external_member_id,
                                  customers.external_member_id),
    deleted_at = NULL,
    updated_at = now()
  RETURNING id
)
SELECT u.id, COALESCE(p.deleted_at IS NOT NULL, false) AS revived
FROM upserted u LEFT JOIN prev p ON p.id = u.id
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
ON CONFLICT (source, external_order_id)
  WHERE source IS NOT NULL AND external_order_id IS NOT NULL
DO NOTHING
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
ON CONFLICT (source, source_event_id)
  WHERE source_event_id IS NOT NULL
DO NOTHING
RETURNING id
"""

STAGED_OK_SQL = """
SELECT row_number, raw FROM staging_rows
WHERE upload_job_id = $1 AND status = 'ok' ORDER BY row_number
"""

QUARANTINE_INSERT_SQL = """
INSERT INTO quarantine_rows (upload_job_id, row_number, source, key,
                             reason, raw, code)
VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
ON CONFLICT (upload_job_id, row_number) DO NOTHING
"""

# A row that now applies cleanly retires the quarantine record that blocked it
# earlier (`(source, key)` is the row's dedup identity, so the match is exact).
RESOLVE_QUARANTINE_SQL = """
UPDATE quarantine_rows SET resolved_at = now()
WHERE source = $1 AND key = $2 AND resolved_at IS NULL
  AND upload_job_id <> $3
"""

# LD1: side-table registry (table, pk column). Writers merge jsonb
# patches key-by-key; D1 column_map leftovers call upsert_custom_data.
CUSTOM_DATA_TABLES = {
    "customers": ("customer_custom_data", "customer_id"),
    "orders": ("order_custom_data", "order_id"),
    "events": ("event_custom_data", "event_id"),
    "products": ("product_custom_data", "product_id"),
}


def custom_data_upsert_sql(entity: str) -> str:
    """INSERT..ON CONFLICT merge for an LD1 side table."""
    try:
        table, pk = CUSTOM_DATA_TABLES[entity]
    except KeyError:
        raise ValueError(
            f"unknown custom-data entity: {entity!r} "
            f"(expected one of {sorted(CUSTOM_DATA_TABLES)})") from None
    return (
        f"INSERT INTO {table} ({pk}, data) "
        f"VALUES ($1, $2::jsonb) "
        f"ON CONFLICT ({pk}) DO UPDATE SET "
        f"data = {table}.data || EXCLUDED.data, "
        f"updated_at = now()"
    )


async def upsert_custom_data(conn, entity: str, entity_id: str,
                             patch: dict) -> None:
    """Merge a jsonb patch into an LD1 side row (insert if missing)."""
    import json

    if not isinstance(patch, dict) or not patch:
        raise ValueError("custom-data patch must be a non-empty mapping")
    await conn.execute(custom_data_upsert_sql(entity), entity_id,
                       json.dumps(patch))

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


# The business zone, as a fixed offset: pgembed ships no tzdata, so the
# cohort/KPI engines write `AT TIME ZONE interval '8 hours'` and this module
# must not name 'Asia/Hong_Kong' either.
BUSINESS_TZ_OFFSET_HOURS = 8
BUSINESS_TZ = timezone(timedelta(hours=BUSINESS_TZ_OFFSET_HOURS))


def _ordered_at(raw: dict,
                feed_tz: Optional[timezone] = BUSINESS_TZ) -> datetime:
    """Feed `ordered_at` -> aware datetime. Raises ValueError (quarantine).

    An unparseable value is refused, never replaced by `now()`: `now()` files
    a bad row under today's revenue, which no later reader can tell from a
    real one. A tz-naive value is read in the zone the feed declares
    (`feed_tz`, the business zone by default) rather than as UTC -- assuming
    UTC shifts a local timestamp by the offset and mis-buckets it in every
    day/week/month figure. `feed_tz=None` means the feed declares no zone,
    and then a naive value is refused as well rather than guessed at.
    """
    val = (raw.get("ordered_at") or "").strip() if raw.get("ordered_at") else ""
    if not val:
        return _now()
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        raise RowRejected(
            f"ordered_at {val!r} is not an ISO-8601 timestamp (refusing to"
            " file the row under now())", "ordered_at_unparseable") from None
    if dt.tzinfo is None:
        if feed_tz is None:
            raise RowRejected(
                f"ordered_at {val!r} is timezone-naive and this feed declares"
                " no zone (8 h of slack would move it between business days)",
                "ordered_at_naive")
        dt = dt.replace(tzinfo=feed_tz)
    return dt


async def _apply_customer(conn, norm: dict, raw: dict) -> tuple[str, bool]:
    """Upsert the member row. Returns `(customer_id, revived)`.

    `revived` is true when the row was a soft-deleted tombstone and this
    ingest brought it back: the feed is authoritative for "this member exists
    again", and a tombstone that stays invisible silently quarantines every
    later order for that member (the resolver only sees `deleted_at IS NULL`).
    """
    row = await conn.fetchrow(
        CUSTOMER_UPSERT_SQL, norm["member_id"], norm["full_name"],
        norm.get("external_member_id"))
    cid = str(row["id"])
    revived = bool(row["revived"])
    for kind in ("phone", "email"):
        val = (raw.get(kind) or "").strip()
        if not val:
            continue
        normed = val.lower() if kind == "email" else val
        await conn.execute(CONTACT_INSERT_SQL, cid, kind, normed, val)
    return cid, revived


async def _apply_identity(conn, norm: dict, raw: dict) -> str:
    tag = await conn.execute(
        IDENTITY_BIND_SQL, norm["member_id"], norm["external_member_id"])
    if tag.split()[-1] == "0":
        raise RowMissing(f"unknown member_id {norm['member_id']}",
                         "member_missing")
    cid = await resolve_customer(conn, norm["member_id"])
    assert cid is not None
    return cid


async def _apply_order(conn, norm: dict, raw: dict, source: str,
                       feed_tz: Optional[timezone] = BUSINESS_TZ
                       ) -> Optional[str]:
    ext = norm["external_order_id"]
    if await conn.fetchval(FIND_ORDER_SQL, source, ext):
        return None  # replay: already applied
    # Parse the timestamp before touching anything else: an unparseable or
    # zoneless value is a quarantine, and it must not depend on state.
    ordered_at = _ordered_at(raw, feed_tz)
    store_id = await resolve_store(conn, norm["store_id"])
    if store_id is None:
        raise RowMissing(f"unknown store {norm['store_id']!r}",
                         "store_missing")
    cid = await resolve_customer(
        conn, raw.get("customer_id"), raw.get("member_id"),
        raw.get("external_member_id"))
    if cid is None:
        raise RowMissing("unresolvable customer (need customer_id, "
                         "member_id or external_member_id)",
                         "customer_unresolved")
    product_ids: list[tuple[str, int, int]] = []
    for line in norm["lines"]:
        pid = await conn.fetchval(RESOLVE_PRODUCT_SQL, line["sku"])
        if pid is None:
            raise RowMissing(f"unknown sku {line['sku']!r}", "sku_missing")
        product_ids.append((str(pid), line["qty"], line["unit_cents"]))
    oid = await conn.fetchval(
        INSERT_ORDER_SQL, cid, store_id, ordered_at,
        norm["total_cents"], (raw.get("status") or "completed")[:20],
        source, ext)
    if oid is None:
        # The partial unique index caught a row a concurrent apply inserted
        # between the find above and this insert. One replay lost the race:
        # skip it (the order and its lines exist) instead of failing the
        # whole job on a uniqueness violation.
        return None
    oid = str(oid)
    for pid, qty, cents in product_ids:
        await conn.execute(INSERT_LINE_SQL, oid, pid, qty, cents)
    await _write_order_placed(conn, cid, store_id, oid,
                              ordered_at, norm["total_cents"], ext)
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
    new_id = await conn.fetchval(INSERT_EVENT_SQL, cid, DERIVED_ORDER_KIND,
                                 DERIVED_EVENT_SOURCE, evid, summary, payload,
                                 ordered_at)
    return None if new_id is None else evid


async def _apply_event(conn, norm: dict, raw: dict,
                       source: str) -> Optional[str]:
    if await conn.fetchval(FIND_EVENT_SQL, source,
                           norm["source_event_id"]):
        return None  # replay: already applied
    cid = await resolve_customer(
        conn, norm.get("customer_id"), raw.get("member_id"),
        raw.get("external_member_id"))
    if cid is None:
        raise RowMissing("unresolvable customer (need customer_id, "
                         "member_id or external_member_id)",
                         "customer_unresolved")
    occurred = norm.get("occurred_at") or _now()
    if isinstance(occurred, str):
        occurred = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
    summary = (raw.get("summary") or f"{norm['type']} event")[:200]
    new_id = await conn.fetchval(
        INSERT_EVENT_SQL, cid, norm["type"], source,
        norm["source_event_id"], summary,
        json.dumps(norm.get("payload") or {}), occurred)
    if new_id is None:
        return None  # lost the race to a concurrent apply of the same event
    return "event"


_APPLIERS = {
    "customers": _apply_customer,
    "orders": _apply_order,
    "events": _apply_event,
    "identity": _apply_identity,
}


async def apply_job(conn, job_id: str, kind: str, source: str,
                    *, feed_tz: Optional[timezone] = BUSINESS_TZ
                    ) -> dict[str, Any]:
    """Apply all `ok` staged rows of a job to core tables.

    Runs inside the caller's transaction. Apply-fails are written to
    `quarantine_rows` (and staged rows marked `rejected`) so the caller
    can reconcile: committed = applied + skipped,
    quarantined += failed. `customers` may revive soft-deleted members:
    the count comes back as `customers_revived` and each revival is also
    written as the staged row's reason, so the run shows it.
    """
    if kind not in _APPLIERS:
        raise ValueError(f"unknown kind {kind!r}")
    applier = _APPLIERS[kind]
    applied = 0
    skipped = 0
    revived_count = 0
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
            code = code_of(exc)
            failed.append({"row_number": row_no, "reason": reason,
                           "code": code})
            await conn.execute(QUARANTINE_INSERT_SQL, job_id, row_no,
                               source, up.row_key(raw, kind), reason,
                               json.dumps(raw), code)
            await conn.execute(MARK_STAGED_SQL, job_id, row_no,
                               "rejected", reason)
            continue
        revived = False
        try:
            if kind == "orders":
                result = await applier(conn, norm, raw, source,
                                       feed_tz=feed_tz)
            elif kind == "events":
                result = await applier(conn, norm, raw, source)
            elif kind == "customers":
                result, revived = await applier(conn, norm, raw)
            else:
                result = await applier(conn, norm, raw)
        except (LookupError, ValueError) as exc:
            reason = f"apply failed: {exc}"
            code = code_of(exc)
            failed.append({"row_number": row_no, "reason": reason,
                           "code": code})
            await conn.execute(QUARANTINE_INSERT_SQL, job_id, row_no,
                               source, up.row_key(raw, kind), reason,
                               json.dumps(raw), code)
            await conn.execute(MARK_STAGED_SQL, job_id, row_no,
                               "rejected", reason)
            continue
        if result is None:
            skipped += 1
        else:
            applied += 1
            key = up.row_key(raw, kind)
            if key:
                await conn.execute(RESOLVE_QUARANTINE_SQL, source, key,
                                   job_id)
        if revived:
            revived_count += 1
            # One audit line per revival, on the row that caused it: a
            # resurrected customer reappears in every segment and dashboard,
            # so the run has to say so where a human reads the job.
            await conn.execute(
                MARK_STAGED_SQL, job_id, row_no, "ok",
                f"revived soft-deleted customer {norm['member_id']}:"
                " re-ingest is authoritative")
        # success: row stays 'ok' (no new status needed — the CHECK enum
        # is ('pending','ok','rejected')); replay safety comes from the
        # per-kind dedup pre-checks (orders/events skip) and convergent
        # upserts (customers/identity), not from row state.
    return {"applied": applied, "skipped": skipped, "failed": failed,
            "customers_revived": revived_count}
