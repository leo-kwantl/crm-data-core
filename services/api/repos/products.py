"""Raw-SQL repo for the product catalogue + per-store inventory.

Pure SQL via asyncpg, no ORM.

Cursor = base64url JSON {"at": <created_at iso>, "id": <uuid>} keying
(created_at DESC, id DESC) pagination. Products are non-PII so LIKE
matching on name/sku is allowed (unlike customer PII search).

Enrichment (migration 008): description / specs (free-form JSONB, e.g.
lens_width, bridge, temple, color, material) / image_url / is_active, plus
the inventory table (one qty row per product x store). Product GETs embed
stock_by_store (store_code order) + total_stock.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime

import asyncpg

PRODUCT_COLS = (
    "id, sku, name, category, price_cents,"
    " description, specs, image_url, is_active, created_at"
)

def normalize_product_filters(
    category: str | None, is_active: bool | None
) -> tuple[str | None, bool | None]:
    """Clean additive catalogue filters (exact category, active flag)."""
    cat = category.strip() if category and category.strip() else None
    return cat, is_active


LIST_PRODUCTS_SQL = """
SELECT id, sku, name, category, price_cents,
       description, specs, image_url, is_active, created_at
FROM products
WHERE ($1::text IS NULL OR name ILIKE '%' || $1 || '%' OR sku ILIKE '%' || $1 || '%')
  AND ($5::text IS NULL OR category = $5)
  AND ($6::boolean IS NULL OR is_active = $6)
  AND ($2::timestamptz IS NULL OR (created_at, id) < ($2::timestamptz, $3::uuid))
ORDER BY created_at DESC, id DESC
LIMIT $4
"""

GET_PRODUCT_SQL = """
SELECT id, sku, name, category, price_cents,
       description, specs, image_url, is_active, created_at
FROM products
WHERE id = $1
"""

CREATE_PRODUCT_SQL = """
INSERT INTO products (sku, name, category, price_cents)
VALUES ($1, $2, $3, $4)
RETURNING id, sku, name, category, price_cents,
          description, specs, image_url, is_active, created_at
"""

STOCK_FOR_PRODUCTS_SQL = """
SELECT i.id, i.product_id, i.store_id, s.code AS store_code, s.name AS store_name,
       i.qty, i.updated_at
FROM inventory i
JOIN stores s ON s.id = i.store_id
WHERE i.product_id = ANY($1::uuid[])
ORDER BY s.code ASC
"""


class DuplicateSkuError(Exception):
    pass


def encode_cursor(at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps({"at": at.isoformat(), "id": str(row_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Return (created_at, id). Raises ValueError on malformed cursors."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(payload["at"]), uuid.UUID(payload["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_cursor") from exc


def _product_row(r) -> dict:  # type: ignore[no-untyped-def]
    d = dict(r)
    specs = d.get("specs")
    if isinstance(specs, str):
        try:
            d["specs"] = json.loads(specs)
        except json.JSONDecodeError:
            d["specs"] = {}
    if d.get("specs") is None:
        d["specs"] = {}
    return d


async def _attach_stock(
    conn: asyncpg.Connection, items: list[dict]
) -> list[dict]:
    """Embed stock_by_store (store_code order) + total_stock into each item."""
    if not items:
        return items
    ids = [i["id"] for i in items]
    rows = await conn.fetch(STOCK_FOR_PRODUCTS_SQL, ids)
    by_product: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["product_id"] = str(d["product_id"])
        d["store_id"] = str(d["store_id"])
        by_product.setdefault(d["product_id"], []).append(d)
    for item in items:
        stock = by_product.get(str(item["id"]), [])
        item["stock_by_store"] = stock
        item["total_stock"] = sum(s["qty"] for s in stock)
    return items


async def list_products(
    conn: asyncpg.Connection,
    *,
    q: str | None,
    limit: int,
    cursor: str | None,
    category: str | None = None,
    is_active: bool | None = None,
) -> tuple[list[dict], str | None]:
    cursor_at: datetime | None = None
    cursor_id: uuid.UUID | None = None
    if cursor:
        cursor_at, cursor_id = decode_cursor(cursor)
    term = q.strip() if q and q.strip() else None
    cat, active = normalize_product_filters(category, is_active)
    rows = await conn.fetch(
        LIST_PRODUCTS_SQL, term, cursor_at, cursor_id, limit + 1, cat, active
    )
    result = [_product_row(r) for r in rows]
    next_cursor = None
    if len(result) > limit:
        result.pop()  # overflow row, only proves a next page exists
        last = result[-1]
        next_cursor = encode_cursor(last["created_at"], last["id"])
    return await _attach_stock(conn, result), next_cursor


async def get_product(conn: asyncpg.Connection, product_id: uuid.UUID) -> dict | None:
    row = await conn.fetchrow(GET_PRODUCT_SQL, product_id)
    if row is None:
        return None
    (item,) = await _attach_stock(conn, [_product_row(row)])
    return item


async def create_product(
    conn: asyncpg.Connection,
    *,
    sku: str,
    name: str,
    category: str,
    price_cents: int,
) -> dict:
    """Insert a catalogue row (enrichment columns take their DB defaults).
    Raises DuplicateSkuError on sku conflict."""
    try:
        row = await conn.fetchrow(CREATE_PRODUCT_SQL, sku, name, category, price_cents)
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateSkuError(sku) from exc
    assert row is not None
    item = _product_row(row)
    item["stock_by_store"] = []
    item["total_stock"] = 0
    return item


async def update_product(
    conn: asyncpg.Connection,
    product_id: uuid.UUID,
    *,
    name: str | None = None,
    category: str | None = None,
    price_cents: int | None = None,
    description: str | None = None,
    description_set: bool = False,
    specs: dict | None = None,
    specs_set: bool = False,
    image_url: str | None = None,
    image_url_set: bool = False,
    is_active: bool | None = None,
) -> dict | None:
    """Partial edit (sku immutable). Explicit null clears description /
    image_url; explicit null specs resets to {}. Returns None when unknown."""
    async with conn.transaction():
        exists = await conn.fetchval(
            "SELECT 1 FROM products WHERE id = $1", product_id
        )
        if exists is None:
            return None
        # Single dynamic UPDATE: same partial-edit semantics (explicit
        # null clears, *_set flags gate), one round-trip instead of up
        # to seven. Unchanged fields are simply absent from SET.
        assignments: list[str] = []
        params: list = [product_id]
        if name is not None:
            params.append(name)
            assignments.append(f"name = ${len(params)}")
        if category is not None:
            params.append(category)
            assignments.append(f"category = ${len(params)}")
        if price_cents is not None:
            params.append(price_cents)
            assignments.append(f"price_cents = ${len(params)}")
        if description_set:
            params.append(description)
            assignments.append(f"description = ${len(params)}")
        if specs_set:
            params.append(json.dumps(specs if specs is not None else {}))
            assignments.append(f"specs = ${len(params)}::jsonb")
        if image_url_set:
            params.append(image_url)
            assignments.append(f"image_url = ${len(params)}")
        if is_active is not None:
            params.append(bool(is_active))
            assignments.append(f"is_active = ${len(params)}")
        if assignments:
            await conn.execute(
                f"UPDATE products SET {', '.join(assignments)} WHERE id = $1",
                *params,
            )
        row = await conn.fetchrow(GET_PRODUCT_SQL, product_id)
        assert row is not None
        (item,) = await _attach_stock(conn, [_product_row(row)])
        return item


# --- Numbered (offset) page: same newest-first order, additive ---

COUNT_PRODUCTS_SQL = """
SELECT COUNT(*)
FROM products
WHERE ($1::text IS NULL OR name ILIKE '%' || $1 || '%' OR sku ILIKE '%' || $1 || '%')
  AND ($2::text IS NULL OR category = $2)
  AND ($3::boolean IS NULL OR is_active = $3)
"""

OFFSET_PRODUCTS_SQL = """
SELECT id, sku, name, category, price_cents,
       description, specs, image_url, is_active, created_at
FROM products
WHERE ($1::text IS NULL OR name ILIKE '%' || $1 || '%' OR sku ILIKE '%' || $1 || '%')
  AND ($4::text IS NULL OR category = $4)
  AND ($5::boolean IS NULL OR is_active = $5)
ORDER BY created_at DESC, id DESC
LIMIT $2 OFFSET $3
"""


async def list_products_offset(
    conn: asyncpg.Connection,
    *,
    q: str | None,
    page: int,
    page_size: int,
    category: str | None = None,
    is_active: bool | None = None,
) -> dict:
    """Numbered page (1-based) of the catalogue, honouring q + filters."""
    from .paging import offset_payload

    term = q.strip() if q and q.strip() else None
    cat, active = normalize_product_filters(category, is_active)
    total = await conn.fetchval(COUNT_PRODUCTS_SQL, term, cat, active)
    rows = await conn.fetch(
        OFFSET_PRODUCTS_SQL, term, page_size, (page - 1) * page_size, cat, active
    )
    items = await _attach_stock(conn, [_product_row(r) for r in rows])
    return offset_payload(items, int(total or 0), page, page_size)
