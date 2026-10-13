"""Store repository: CRUD + cursor list (raw SQL, asyncpg, no ORM).

Cursor = base64url JSON {"at": <created_at iso>, "id": <uuid>} keying
(created_at DESC, id DESC), newest-first (same convention as orders/identity).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from uuid import UUID

import asyncpg


class DuplicateCodeError(Exception):
    pass


class LinkedError(Exception):
    """Store row still referenced by orders.store_id (DELETE blocked -> 409)."""


def encode_cursor(at: datetime, row_id: UUID) -> str:
    raw = json.dumps({"at": at.isoformat(), "id": str(row_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Return (created_at, id). Raises ValueError on malformed cursors."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(payload["at"]), UUID(payload["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_cursor") from exc


def _row(r) -> dict:  # type: ignore[no-untyped-def]
    d = dict(r)
    d["id"] = str(d["id"])
    for key in ("brand_id", "group_id"):
        if d.get(key) is not None:
            d[key] = str(d[key])
    return d


async def create_store(
    pool: asyncpg.Pool,
    *,
    code: str,
    name: str,
    address: str | None,
    phone: str | None,
    brand_id: UUID,
) -> dict:
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO stores (code, name, address, phone, brand_id)"
                " VALUES ($1, $2, $3, $4, $5)"
                " RETURNING id, code, name, address, phone, brand_id,"
                " (SELECT code FROM brands WHERE id = $5) AS brand_code,"
                " created_at",
                code.strip(),
                name.strip(),
                address,
                phone,
                brand_id,
            )
        except asyncpg.UniqueViolationError as exc:
            if "stores_code" in str(exc).lower() or "code" in str(exc).lower():
                raise DuplicateCodeError(str(exc))
            raise
        return _row(row)


async def get_store(pool: asyncpg.Pool, store_id: UUID) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT s.id, s.code, s.name, s.address, s.phone, s.created_at,"
            " s.brand_id, b.code AS brand_code, b.group_id,"
            " (SELECT COUNT(*) FROM orders o WHERE o.store_id = s.id) AS order_count"
            " FROM stores s LEFT JOIN brands b ON b.id = s.brand_id"
            " WHERE s.id = $1",
            store_id,
        )
        if row is None:
            return None
        return _row(row)


async def list_stores_page(
    pool: asyncpg.Pool,
    *,
    limit: int,
    cursor: str | None,
    brand_id: UUID | None = None,
    group_id: UUID | None = None,
    q: str | None = None,
) -> tuple[list[dict], str | None]:
    """Brand filter: exact match when a session brand is set; group filter
    scopes group-wide roles to their own group (NULL when HQ brand-less).
    q matches code / name (ILIKE, non-PII)."""
    cursor_at: datetime | None = None
    cursor_id: UUID | None = None
    if cursor:
        cursor_at, cursor_id = decode_cursor(cursor)
    term = q.strip() if q and q.strip() else None
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT s.id, s.code, s.name, s.address, s.phone, s.created_at,"
            " s.brand_id, b.code AS brand_code"
            " FROM stores s LEFT JOIN brands b ON b.id = s.brand_id"
            " WHERE ($1::timestamptz IS NULL"
            "   OR (s.created_at, s.id) < ($1::timestamptz, $2::uuid))"
            "   AND ($4::uuid IS NULL OR s.brand_id = $4::uuid)"
            "   AND ($5::uuid IS NULL OR b.group_id = $5::uuid)"
            "   AND ($6::text IS NULL OR (s.code ILIKE '%' || $6 || '%'"
            "     OR s.name ILIKE '%' || $6 || '%'))"
            " ORDER BY s.created_at DESC, s.id DESC LIMIT $3",
            cursor_at,
            cursor_id,
            limit + 1,
            brand_id,
            group_id,
            term,
        )
    result = [_row(r) for r in rows]
    next_cursor = None
    if len(result) > limit:
        result.pop()  # overflow row, only proves a next page exists
        last = result[-1]
        next_cursor = encode_cursor(last["created_at"], UUID(str(last["id"])))
    return result, next_cursor


async def update_store(
    pool: asyncpg.Pool,
    store_id: UUID,
    *,
    name: str | None,
    address: str | None,
    clear_address: bool,
    phone: str | None,
    clear_phone: bool,
) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM stores WHERE id = $1", store_id)
        if row is None:
            return None
        if name is not None:
            await conn.execute(
                "UPDATE stores SET name = $2 WHERE id = $1", store_id, name.strip()
            )
        if clear_address:
            await conn.execute(
                "UPDATE stores SET address = NULL WHERE id = $1", store_id
            )
        elif address is not None:
            await conn.execute(
                "UPDATE stores SET address = $2 WHERE id = $1", store_id, address
            )
        if clear_phone:
            await conn.execute("UPDATE stores SET phone = NULL WHERE id = $1", store_id)
        elif phone is not None:
            await conn.execute(
                "UPDATE stores SET phone = $2 WHERE id = $1", store_id, phone
            )
        updated = await conn.fetchrow(
            "SELECT s.id, s.code, s.name, s.address, s.phone, s.created_at,"
            " s.brand_id, b.code AS brand_code, b.group_id,"
            " (SELECT COUNT(*) FROM orders o WHERE o.store_id = s.id) AS order_count"
            " FROM stores s LEFT JOIN brands b ON b.id = s.brand_id"
            " WHERE s.id = $1",
            store_id,
        )
        return _row(updated)


async def list_stores_offset(
    pool: asyncpg.Pool,
    *,
    page: int,
    page_size: int,
    brand_id: UUID | None = None,
    group_id: UUID | None = None,
    q: str | None = None,
) -> dict:
    """Numbered page (1-based) of stores, newest-first (brand/group scoped,
    q matches code / name)."""
    from .paging import offset_payload

    term = q.strip() if q and q.strip() else None
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM stores s LEFT JOIN brands b ON b.id = s.brand_id"
            " WHERE ($1::uuid IS NULL OR s.brand_id = $1::uuid)"
            "   AND ($2::uuid IS NULL OR b.group_id = $2::uuid)"
            "   AND ($3::text IS NULL OR (s.code ILIKE '%' || $3 || '%'"
            "     OR s.name ILIKE '%' || $3 || '%'))",
            brand_id,
            group_id,
            term,
        )
        rows = await conn.fetch(
            "SELECT s.id, s.code, s.name, s.address, s.phone, s.created_at,"
            " s.brand_id, b.code AS brand_code"
            " FROM stores s LEFT JOIN brands b ON b.id = s.brand_id"
            " WHERE ($3::uuid IS NULL OR s.brand_id = $3::uuid)"
            "   AND ($4::uuid IS NULL OR b.group_id = $4::uuid)"
            "   AND ($5::text IS NULL OR (s.code ILIKE '%' || $5 || '%'"
            "     OR s.name ILIKE '%' || $5 || '%'))"
            " ORDER BY s.created_at DESC, s.id DESC LIMIT $1 OFFSET $2",
            page_size,
            (page - 1) * page_size,
            brand_id,
            group_id,
            term,
        )
        items = [_row(r) for r in rows]
    return offset_payload(items, int(total or 0), page, page_size)


async def delete_store(pool: asyncpg.Pool, store_id: UUID) -> bool | None:
    """Delete a store. Returns None when missing; raises LinkedError when
    orders reference it (caller maps to 409)."""
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM stores WHERE id = $1", store_id)
        if exists is None:
            return None
        linked = await conn.fetchval(
            "SELECT 1 FROM orders WHERE store_id = $1 LIMIT 1", store_id
        )
        if linked is not None:
            raise LinkedError("store_has_orders")
        await conn.execute("DELETE FROM stores WHERE id = $1", store_id)
        return True
