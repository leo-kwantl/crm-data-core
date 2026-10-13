"""Raw-SQL repo for per-store inventory. Pure SQL via asyncpg, no ORM.

One row per (product_id, store_id); PUT upserts (insert or replace qty).
Product delete cascades (FK); store delete is blocked by RESTRICT while
stock rows reference it (store DELETE maps that to 409 downstream).
"""

from __future__ import annotations

import uuid

import asyncpg

LIST_INVENTORY_SQL = """
SELECT i.id, i.product_id, i.store_id, s.code AS store_code, s.name AS store_name,
       i.qty, i.updated_at
FROM inventory i
JOIN stores s ON s.id = i.store_id
WHERE i.product_id = $1
ORDER BY s.code ASC
"""

UPSERT_INVENTORY_SQL = """
INSERT INTO inventory (product_id, store_id, qty)
VALUES ($1, $2, $3)
ON CONFLICT (product_id, store_id)
DO UPDATE SET qty = EXCLUDED.qty, updated_at = now()
RETURNING id, product_id, store_id, qty, updated_at
"""


def _row(r) -> dict:  # type: ignore[no-untyped-def]
    d = dict(r)
    d["id"] = str(d["id"])
    d["product_id"] = str(d["product_id"])
    d["store_id"] = str(d["store_id"])
    return d


async def product_exists(conn: asyncpg.Connection, product_id: uuid.UUID) -> bool:
    return (
        await conn.fetchval("SELECT 1 FROM products WHERE id = $1", product_id)
        is not None
    )


async def store_exists(conn: asyncpg.Connection, store_id: uuid.UUID) -> bool:
    return (
        await conn.fetchval("SELECT 1 FROM stores WHERE id = $1", store_id) is not None
    )


async def list_inventory(
    conn: asyncpg.Connection, product_id: uuid.UUID
) -> list[dict] | None:
    """Stock rows for one product (store_code order), or None when unknown."""
    if not await product_exists(conn, product_id):
        return None
    rows = await conn.fetch(LIST_INVENTORY_SQL, product_id)
    return [_row(r) for r in rows]


async def upsert_inventory(
    conn: asyncpg.Connection,
    *,
    product_id: uuid.UUID,
    store_id: uuid.UUID,
    qty: int,
) -> dict | tuple[None, str]:
    """Insert or replace the (product, store) qty row.

    Returns the row, or (None, 'product_not_found' | 'store_not_found')."""
    if not await product_exists(conn, product_id):
        return None, "product_not_found"
    if not await store_exists(conn, store_id):
        return None, "store_not_found"
    row = await conn.fetchrow(UPSERT_INVENTORY_SQL, product_id, store_id, qty)
    assert row is not None
    item = _row(row)
    store = await conn.fetchrow(
        "SELECT code, name FROM stores WHERE id = $1", store_id
    )
    assert store is not None
    item["store_code"] = store["code"]
    item["store_name"] = store["name"]
    return item
