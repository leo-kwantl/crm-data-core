"""Raw-SQL repo for purchase history. Pure SQL via asyncpg, no ORM.

Cursor = base64url JSON {"at": <ordered_at iso>, "id": <uuid>} keying
(ordered_at DESC, id DESC) pagination, newest-first.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime

import asyncpg

def normalize_order_filters(
    store_code: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    status: str | None = None,
    q: str | None = None,
) -> tuple[str | None, datetime | None, datetime | None, str | None, str | None]:
    """Clean additive order-list filters. date_* arrive as date or datetime.

    status is an exact match; q matches the order UUID text (prefix) plus
    the customer member_id / full_name (ILIKE, non-PII)."""
    store = store_code.strip() if store_code and store_code.strip() else None
    st = status.strip() if status and status.strip() else None
    term = q.strip() if q and q.strip() else None
    return store, date_from, date_to, st, term


LIST_ORDERS_SQL = """
SELECT o.id, o.customer_id, o.store_id, s.code AS store_code, s.name AS store_name,
       o.ordered_at, o.total_cents, o.status,
       (SELECT COUNT(*) FROM order_items i WHERE i.order_id = o.id) AS item_count
FROM orders o
JOIN stores s ON s.id = o.store_id
WHERE o.customer_id = $1
  AND ($5::text IS NULL OR s.code = $5)
  AND ($6::date IS NULL OR o.ordered_at >= $6::date::timestamptz)
  AND ($7::date IS NULL OR o.ordered_at < (($7::date + 1)::timestamptz))
  AND ($2::timestamptz IS NULL OR (o.ordered_at, o.id) < ($2::timestamptz, $3::uuid))
  AND ($8::uuid IS NULL OR s.brand_id = $8::uuid)
ORDER BY o.ordered_at DESC, o.id DESC
LIMIT $4
"""

ORDER_HEADER_SQL = """
SELECT o.id, o.customer_id, o.store_id, o.ordered_at, o.total_cents, o.status,
       s.code AS store_code, s.name AS store_name
FROM orders o
JOIN stores s ON s.id = o.store_id
WHERE o.id = $1
  AND ($2::uuid IS NULL OR s.brand_id = $2::uuid)
"""

ORDER_ITEMS_SQL = """
SELECT i.id, i.order_id, i.product_id, i.qty, i.unit_price_cents,
       p.name AS product_name, p.sku AS sku
FROM order_items i
JOIN products p ON p.id = i.product_id
WHERE i.order_id = $1
ORDER BY i.id
"""

CUSTOMER_EXISTS_SQL = "SELECT 1 FROM customers WHERE id = $1 AND deleted_at IS NULL"


def encode_cursor(at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps({"at": at.isoformat(), "id": str(row_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Return (ordered_at, id). Raises ValueError on malformed cursors."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(payload["at"]), uuid.UUID(payload["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_cursor") from exc


async def customer_exists(conn: asyncpg.Connection, customer_id: uuid.UUID) -> bool:
    return await conn.fetchval(CUSTOMER_EXISTS_SQL, customer_id) is not None


async def list_customer_orders(
    conn: asyncpg.Connection,
    customer_id: uuid.UUID,
    *,
    limit: int,
    cursor: str | None,
    store_code: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    brand_id: uuid.UUID | None = None,
) -> tuple[list[dict], str | None]:
    """Newest-first page. Returns (rows, next_cursor); caller fetches limit+1.

    SCOPE: `brand_id` is the session's own brand (deps.session_brand). A
    customer who bought in two brands still sees only their own brand's
    orders: the order belongs to a store, the store belongs to a brand, so
    the predicate is the joined source row's (population form).
    """
    cursor_at: datetime | None = None
    cursor_id: uuid.UUID | None = None
    if cursor:
        cursor_at, cursor_id = decode_cursor(cursor)
    store, dfrom, dto, _, _ = normalize_order_filters(store_code, date_from, date_to)
    rows = await conn.fetch(
        LIST_ORDERS_SQL, customer_id, cursor_at, cursor_id, limit + 1,
        store, dfrom, dto, brand_id,
    )
    result = [dict(r) for r in rows]
    next_cursor = None
    if len(result) > limit:
        extra = result.pop()
        last = result[-1]
        _ = extra  # popped overflow row, only proves a next page exists
        next_cursor = encode_cursor(last["ordered_at"], last["id"])
    return result, next_cursor


async def get_order_with_items(
    conn: asyncpg.Connection,
    order_id: uuid.UUID,
    brand_id: uuid.UUID | None = None,
) -> dict | None:
    """The order header and its items, or None when it is not the session's.

    SCOPE: `brand_id` is the session's own brand (deps.session_brand). An
    order belongs to a store, the store belongs to a brand, so another
    brand's order is not found at all -- the read answers 404, not 403,
    because a 403 would say the order exists.
    """
    header = await conn.fetchrow(ORDER_HEADER_SQL, order_id, brand_id)
    if header is None:
        return None
    items = await conn.fetch(ORDER_ITEMS_SQL, order_id)
    detail = dict(header)
    detail["items"] = [dict(r) for r in items]
    return detail


# --- Global order list (the brand-scoped variant of db/queries/orders.sql:
# --- list_orders_global). The shared file stays GLOBAL because data's
# --- pipeline reads every brand's orders; the app's serving reader carries
# --- the session's brand, so the two are not the same statement. ---

LIST_ORDERS_GLOBAL_SQL = """
SELECT o.id, o.customer_id, c.member_id AS customer_member_id,
       c.full_name AS customer_name,
       o.store_id, s.code AS store_code, s.name AS store_name,
       o.ordered_at, o.total_cents, o.status,
       (SELECT COUNT(*) FROM order_items i WHERE i.order_id = o.id) AS item_count
FROM orders o
JOIN customers c ON c.id = o.customer_id AND c.deleted_at IS NULL
JOIN stores s ON s.id = o.store_id
WHERE ($3::text IS NULL OR s.code = $3)
  AND ($4::date IS NULL OR o.ordered_at >= $4::date::timestamptz)
  AND ($5::date IS NULL OR o.ordered_at < (($5::date + 1)::timestamptz))
  AND ($7::text IS NULL OR o.status = $7)
  AND ($8::text IS NULL OR (o.id::text ILIKE $8 || '%'
    OR c.member_id ILIKE '%' || $8 || '%' OR c.full_name ILIKE '%' || $8 || '%'))
  AND ($1::timestamptz IS NULL OR (o.ordered_at, o.id) < ($1::timestamptz, $2::uuid))
  AND ($9::uuid IS NULL OR s.brand_id = $9::uuid)
ORDER BY o.ordered_at DESC, o.id DESC
LIMIT $6
"""


async def list_orders_global(
    conn: asyncpg.Connection,
    *,
    limit: int,
    cursor: str | None,
    store_code: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    status: str | None = None,
    q: str | None = None,
    brand_id: uuid.UUID | None = None,
) -> tuple[list[dict], str | None]:
    """Newest-first page across the session's scope, with store + customer
    display fields and per-order item count. Returns (rows, next_cursor).

    SCOPE: `brand_id` pins the page to one brand -- the session's own brand
    (see deps.session_brand). None (a group-wide or HQ session) reads every
    brand, which is the only way this list is global. The predicate is the
    joined source row's (an order belongs to a store, a store belongs to a
    brand), so it is the population form: `($9 IS NULL OR s.brand_id = $9)`.
    """
    cursor_at: datetime | None = None
    cursor_id: uuid.UUID | None = None
    if cursor:
        cursor_at, cursor_id = decode_cursor(cursor)
    store, dfrom, dto, st, term = normalize_order_filters(store_code, date_from, date_to, status, q)
    rows = await conn.fetch(
        LIST_ORDERS_GLOBAL_SQL, cursor_at, cursor_id, store, dfrom, dto, limit + 1,
        st, term, brand_id,
    )
    result = [dict(r) for r in rows]
    next_cursor = None
    if len(result) > limit:
        result.pop()  # overflow row, only proves a next page exists
        last = result[-1]
        next_cursor = encode_cursor(last["ordered_at"], last["id"])
    return result, next_cursor


# --- Numbered (offset) pages: same newest-first order, additive ---

COUNT_CUSTOMER_ORDERS_SQL = """
SELECT COUNT(*) FROM orders o JOIN stores s ON s.id = o.store_id
WHERE o.customer_id = $1
  AND ($2::text IS NULL OR s.code = $2)
  AND ($3::date IS NULL OR o.ordered_at >= $3::date::timestamptz)
  AND ($4::date IS NULL OR o.ordered_at < (($4::date + 1)::timestamptz))
  AND ($5::uuid IS NULL OR s.brand_id = $5::uuid)
"""

OFFSET_CUSTOMER_ORDERS_SQL = """
SELECT o.id, o.customer_id, o.store_id, s.code AS store_code, s.name AS store_name,
       o.ordered_at, o.total_cents, o.status,
       (SELECT COUNT(*) FROM order_items i WHERE i.order_id = o.id) AS item_count
FROM orders o
JOIN stores s ON s.id = o.store_id
WHERE o.customer_id = $1
  AND ($4::text IS NULL OR s.code = $4)
  AND ($5::date IS NULL OR o.ordered_at >= $5::date::timestamptz)
  AND ($6::date IS NULL OR o.ordered_at < (($6::date + 1)::timestamptz))
  AND ($7::uuid IS NULL OR s.brand_id = $7::uuid)
ORDER BY o.ordered_at DESC, o.id DESC
LIMIT $2 OFFSET $3
"""

COUNT_ORDERS_GLOBAL_SQL = """
SELECT COUNT(*)
FROM orders o
JOIN customers c ON c.id = o.customer_id AND c.deleted_at IS NULL
JOIN stores s ON s.id = o.store_id
WHERE ($1::text IS NULL OR s.code = $1)
  AND ($2::date IS NULL OR o.ordered_at >= $2::date::timestamptz)
  AND ($3::date IS NULL OR o.ordered_at < (($3::date + 1)::timestamptz))
  AND ($4::text IS NULL OR o.status = $4)
  AND ($5::text IS NULL OR (o.id::text ILIKE $5 || '%'
    OR c.member_id ILIKE '%' || $5 || '%' OR c.full_name ILIKE '%' || $5 || '%'))
  AND ($6::uuid IS NULL OR s.brand_id = $6::uuid)
"""

OFFSET_ORDERS_GLOBAL_SQL = """
SELECT o.id, o.customer_id, c.member_id AS customer_member_id,
       c.full_name AS customer_name,
       o.store_id, s.code AS store_code, s.name AS store_name,
       o.ordered_at, o.total_cents, o.status,
       (SELECT COUNT(*) FROM order_items i WHERE i.order_id = o.id) AS item_count
FROM orders o
JOIN customers c ON c.id = o.customer_id AND c.deleted_at IS NULL
JOIN stores s ON s.id = o.store_id
WHERE ($3::text IS NULL OR s.code = $3)
  AND ($4::date IS NULL OR o.ordered_at >= $4::date::timestamptz)
  AND ($5::date IS NULL OR o.ordered_at < (($5::date + 1)::timestamptz))
  AND ($6::text IS NULL OR o.status = $6)
  AND ($7::text IS NULL OR (o.id::text ILIKE $7 || '%'
    OR c.member_id ILIKE '%' || $7 || '%' OR c.full_name ILIKE '%' || $7 || '%'))
  AND ($8::uuid IS NULL OR s.brand_id = $8::uuid)
ORDER BY o.ordered_at DESC, o.id DESC
LIMIT $1 OFFSET $2
"""


async def list_customer_orders_offset(
    conn: asyncpg.Connection,
    customer_id: uuid.UUID,
    *,
    page: int,
    page_size: int,
    store_code: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    brand_id: uuid.UUID | None = None,
) -> dict:
    """Numbered page (1-based) of one customer's orders (brand-scoped, see
    list_customer_orders)."""
    from .paging import offset_payload

    store, dfrom, dto, _, _ = normalize_order_filters(store_code, date_from, date_to)
    total = await conn.fetchval(
        COUNT_CUSTOMER_ORDERS_SQL, customer_id, store, dfrom, dto, brand_id
    )
    rows = await conn.fetch(
        OFFSET_CUSTOMER_ORDERS_SQL, customer_id, page_size, (page - 1) * page_size,
        store, dfrom, dto, brand_id,
    )
    return offset_payload(
        [dict(r) for r in rows], int(total or 0), page, page_size
    )


async def list_orders_global_offset(
    conn: asyncpg.Connection,
    *,
    page: int,
    page_size: int,
    store_code: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    status: str | None = None,
    q: str | None = None,
    brand_id: uuid.UUID | None = None,
) -> dict:
    """Numbered page (1-based) across the session's scope.

    SCOPE: the same brand pinning as the keyset page above -- `brand_id` is
    the session's own brand, None reads every brand (group-wide/HQ).
    """
    from .paging import offset_payload

    store, dfrom, dto, st, term = normalize_order_filters(
        store_code, date_from, date_to, status, q
    )
    total = await conn.fetchval(
        COUNT_ORDERS_GLOBAL_SQL, store, dfrom, dto, st, term, brand_id
    )
    rows = await conn.fetch(
        OFFSET_ORDERS_GLOBAL_SQL, page_size, (page - 1) * page_size,
        store, dfrom, dto, st, term, brand_id,
    )
    return offset_payload(
        [dict(r) for r in rows], int(total or 0), page, page_size
    )
