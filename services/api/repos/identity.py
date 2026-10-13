"""Identity repository: staff roster + login-account rows (raw SQL, asyncpg).

ACCOUNT RECORDS ONLY — no passwords/sessions; auth deferred to pilot
hardening is done: session auth (x-session-token) guards the routes. Canonical SQL lives in
db/queries/identity.sql; inline statements below mirror it.

Cursor = base64url JSON {"at": <created_at iso>, "id": <uuid>} keying
(created_at DESC, id DESC), newest-first (same convention as orders/lists).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from uuid import UUID

import asyncpg


class DuplicateCodeError(Exception):
    pass


class NotFoundError(Exception):
    pass


class LinkedError(Exception):
    """Staff row still linked from users.staff_id (DELETE blocked -> 409)."""


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


def _staff_row(r) -> dict:  # type: ignore[no-untyped-def]
    d = dict(r)
    d["id"] = str(d["id"])
    if d.get("store_id") is not None:
        d["store_id"] = str(d["store_id"])
    if d.get("brand_id") is not None:
        d["brand_id"] = str(d["brand_id"])
    return d


def _user_row(r) -> dict:  # type: ignore[no-untyped-def]
    d = dict(r)
    d["id"] = str(d["id"])
    if d.get("staff_id") is not None:
        d["staff_id"] = str(d["staff_id"])
    return d


def _is_unique_violation(exc: asyncpg.UniqueViolationError, needle: str) -> bool:
    return needle in str(exc).lower()


# --- staff ---

async def create_staff(
    pool: asyncpg.Pool,
    *,
    staff_code: str,
    full_name: str,
    store_id: UUID | None,
    role: str,
    brand_id: UUID | None = None,
) -> dict:
    """Brand link: explicit brand_id wins; else the home store's brand; else
    NULL (= group-wide, manager/admin only by convention — brand-less sa
    keeps legacy group-wide reads). Raises NotFoundError on bad ids."""
    async with pool.acquire() as conn:
        store_brand = None
        if store_id is not None:
            store_brand = await conn.fetchval(
                "SELECT brand_id FROM stores WHERE id = $1", store_id
            )
            if store_brand is None and await conn.fetchval(
                "SELECT 1 FROM stores WHERE id = $1", store_id
            ) is None:
                raise NotFoundError("store_not_found")
        if brand_id is not None:
            ok = await conn.fetchval(
                "SELECT 1 FROM brands WHERE id = $1", brand_id
            )
            if ok is None:
                raise NotFoundError("brand_not_found")
        else:
            brand_id = store_brand
        try:
            row = await conn.fetchrow(
                "INSERT INTO staff (staff_code, full_name, store_id, role, brand_id)"
                " VALUES ($1, $2, $3, $4, $5)"
                " RETURNING id, staff_code, full_name, store_id, role, brand_id,"
                " joined_at, left_at, created_at, updated_at",
                staff_code.strip(),
                full_name.strip(),
                store_id,
                role.strip() or "sa",
                brand_id,
            )
        except asyncpg.UniqueViolationError as exc:
            if _is_unique_violation(exc, "staff_code"):
                raise DuplicateCodeError(str(exc))
            raise
        row = await conn.fetchrow(
            "SELECT s.id, s.staff_code, s.full_name, s.store_id, st.code AS store_code,"
            " s.role, s.brand_id, b.code AS brand_code,"
            " s.joined_at, s.left_at, s.created_at, s.updated_at"
            " FROM staff s LEFT JOIN stores st ON st.id = s.store_id"
            " LEFT JOIN brands b ON b.id = s.brand_id WHERE s.id = $1",
            row["id"],
        )
        return _staff_row(row)


async def get_staff(pool: asyncpg.Pool, staff_id: UUID) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT s.id, s.staff_code, s.full_name, s.store_id, st.code AS store_code,"
            " s.role, s.brand_id, b.code AS brand_code,"
            " s.joined_at, s.left_at, s.created_at, s.updated_at"
            " FROM staff s LEFT JOIN stores st ON st.id = s.store_id"
            " LEFT JOIN brands b ON b.id = s.brand_id WHERE s.id = $1",
            staff_id,
        )
        return _staff_row(row) if row else None


async def list_staff_page(
    pool: asyncpg.Pool,
    *,
    limit: int,
    cursor: str | None,
    q: str | None = None,
) -> tuple[list[dict], str | None]:
    """Newest-first page (q matches staff_code / full_name, ILIKE)."""
    cursor_at, cursor_id = (decode_cursor(cursor) if cursor else (None, None))
    term = q.strip() if q and q.strip() else None
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT s.id, s.staff_code, s.full_name, s.store_id, st.code AS store_code,"
            " s.role, s.brand_id, b.code AS brand_code,"
            " s.joined_at, s.left_at, s.created_at, s.updated_at"
            " FROM staff s LEFT JOIN stores st ON st.id = s.store_id"
            " LEFT JOIN brands b ON b.id = s.brand_id"
            " WHERE ($1::timestamptz IS NULL OR (s.created_at, s.id) < ($1::timestamptz, $2::uuid))"
            "   AND ($4::text IS NULL OR (s.staff_code ILIKE '%' || $4 || '%'"
            "     OR s.full_name ILIKE '%' || $4 || '%'))"
            " ORDER BY s.created_at DESC, s.id DESC LIMIT $3",
            cursor_at,
            cursor_id,
            limit + 1,
            term,
        )
        page = [_staff_row(r) for r in rows]
    next_cursor = None
    if len(page) > limit:
        page.pop()
        last = page[-1]
        next_cursor = encode_cursor(last["created_at"], UUID(last["id"]))
    return page, next_cursor


async def update_staff(
    pool: asyncpg.Pool,
    staff_id: UUID,
    *,
    full_name: str | None = None,
    store_id: UUID | None = None,
    clear_store: bool = False,
    role: str | None = None,
    left_at: datetime | None = None,
    left_at_set: bool = False,
    brand_id: UUID | None = None,
    brand_id_set: bool = False,
) -> dict | None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval("SELECT 1 FROM staff WHERE id = $1", staff_id)
            if exists is None:
                return None
            if full_name is not None:
                await conn.execute(
                    "UPDATE staff SET full_name = $2, updated_at = now() WHERE id = $1",
                    staff_id,
                    full_name.strip(),
                )
            if clear_store:
                await conn.execute(
                    "UPDATE staff SET store_id = NULL, updated_at = now() WHERE id = $1",
                    staff_id,
                )
            elif store_id is not None:
                ok = await conn.fetchval("SELECT 1 FROM stores WHERE id = $1", store_id)
                if ok is None:
                    raise NotFoundError("store_not_found")
                await conn.execute(
                    "UPDATE staff SET store_id = $2, updated_at = now() WHERE id = $1",
                    staff_id,
                    store_id,
                )
            if role is not None:
                await conn.execute(
                    "UPDATE staff SET role = $2, updated_at = now() WHERE id = $1",
                    staff_id,
                    role.strip() or "sa",
                )
            if brand_id_set:
                if brand_id is not None:
                    ok = await conn.fetchval(
                        "SELECT 1 FROM brands WHERE id = $1", brand_id
                    )
                    if ok is None:
                        raise NotFoundError("brand_not_found")
                await conn.execute(
                    "UPDATE staff SET brand_id = $2, updated_at = now() WHERE id = $1",
                    staff_id,
                    brand_id,
                )
            if left_at_set:
                await conn.execute(
                    "UPDATE staff SET left_at = $2, updated_at = now() WHERE id = $1",
                    staff_id,
                    left_at,
                )
            row = await conn.fetchrow(
                "SELECT s.id, s.staff_code, s.full_name, s.store_id, st.code AS store_code,"
                " s.role, s.brand_id, b.code AS brand_code,"
                " s.joined_at, s.left_at, s.created_at, s.updated_at"
                " FROM staff s LEFT JOIN stores st ON st.id = s.store_id"
                " LEFT JOIN brands b ON b.id = s.brand_id WHERE s.id = $1",
                staff_id,
            )
            return _staff_row(row) if row else None


async def delete_staff(pool: asyncpg.Pool, staff_id: UUID) -> bool:
    """DELETE a staff row. Raises LinkedError when a user links to it (-> 409)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval("SELECT 1 FROM staff WHERE id = $1", staff_id)
            if exists is None:
                return False
            n = await conn.fetchval("SELECT COUNT(*) FROM users WHERE staff_id = $1", staff_id)
            if int(n or 0) > 0:
                raise LinkedError(str(staff_id))
            await conn.execute("DELETE FROM staff WHERE id = $1", staff_id)
            return True


# --- users (account records; NO hard delete: DELETE -> 405 at the router) ---

async def create_user(
    pool: asyncpg.Pool,
    *,
    username: str,
    display_name: str,
    role: str,
    staff_id: UUID | None,
) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            if staff_id is not None:
                ok = await conn.fetchval("SELECT 1 FROM staff WHERE id = $1", staff_id)
                if ok is None:
                    raise NotFoundError("staff_not_found")
                taken = await conn.fetchval(
                    "SELECT 1 FROM users WHERE staff_id = $1", staff_id
                )
                if taken is not None:
                    raise LinkedError("staff_already_linked")
            try:
                row = await conn.fetchrow(
                    "INSERT INTO users (username, display_name, role, staff_id)"
                    " VALUES ($1, $2, $3, $4)"
                    " RETURNING id, username, display_name, role, is_disabled,"
                    " staff_id, created_at, updated_at",
                    username.strip(),
                    display_name.strip(),
                    role.strip() or "sa",
                    staff_id,
                )
            except asyncpg.UniqueViolationError as exc:
                if _is_unique_violation(exc, "username"):
                    raise DuplicateCodeError(str(exc))
                raise
            full = await conn.fetchrow(
                "SELECT u.id, u.username, u.display_name, u.role, u.is_disabled,"
                " u.staff_id, s.staff_code, u.created_at, u.updated_at"
                " FROM users u LEFT JOIN staff s ON s.id = u.staff_id WHERE u.id = $1",
                row["id"],
            )
            return _user_row(full)


async def get_user(pool: asyncpg.Pool, user_id: UUID) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT u.id, u.username, u.display_name, u.role, u.is_disabled,"
            " u.staff_id, s.staff_code, u.created_at, u.updated_at"
            " FROM users u LEFT JOIN staff s ON s.id = u.staff_id WHERE u.id = $1",
            user_id,
        )
        return _user_row(row) if row else None


async def list_users_page(
    pool: asyncpg.Pool,
    *,
    limit: int,
    cursor: str | None,
    q: str | None = None,
) -> tuple[list[dict], str | None]:
    """Newest-first page (q matches username / display_name, ILIKE)."""
    cursor_at, cursor_id = (decode_cursor(cursor) if cursor else (None, None))
    term = q.strip() if q and q.strip() else None
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT u.id, u.username, u.display_name, u.role, u.is_disabled,"
            " u.staff_id, s.staff_code, u.created_at, u.updated_at"
            " FROM users u LEFT JOIN staff s ON s.id = u.staff_id"
            " WHERE ($1::timestamptz IS NULL OR (u.created_at, u.id) < ($1::timestamptz, $2::uuid))"
            "   AND ($4::text IS NULL OR (u.username ILIKE '%' || $4 || '%'"
            "     OR u.display_name ILIKE '%' || $4 || '%'))"
            " ORDER BY u.created_at DESC, u.id DESC LIMIT $3",
            cursor_at,
            cursor_id,
            limit + 1,
            term,
        )
        page = [_user_row(r) for r in rows]
    next_cursor = None
    if len(page) > limit:
        page.pop()
        last = page[-1]
        next_cursor = encode_cursor(last["created_at"], UUID(last["id"]))
    return page, next_cursor


async def update_user(
    pool: asyncpg.Pool,
    user_id: UUID,
    *,
    display_name: str | None = None,
    role: str | None = None,
    is_disabled: bool | None = None,
    staff_id: UUID | None = None,
    clear_staff: bool = False,
    staff_set: bool = False,
) -> dict | None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval("SELECT 1 FROM users WHERE id = $1", user_id)
            if exists is None:
                return None
            if display_name is not None:
                await conn.execute(
                    "UPDATE users SET display_name = $2, updated_at = now() WHERE id = $1",
                    user_id,
                    display_name.strip(),
                )
            if role is not None:
                await conn.execute(
                    "UPDATE users SET role = $2, updated_at = now() WHERE id = $1",
                    user_id,
                    role.strip() or "sa",
                )
            if is_disabled is not None:
                await conn.execute(
                    "UPDATE users SET is_disabled = $2, updated_at = now() WHERE id = $1",
                    user_id,
                    bool(is_disabled),
                )
            if clear_staff:
                await conn.execute(
                    "UPDATE users SET staff_id = NULL, updated_at = now() WHERE id = $1",
                    user_id,
                )
            elif staff_set and staff_id is not None:
                ok = await conn.fetchval("SELECT 1 FROM staff WHERE id = $1", staff_id)
                if ok is None:
                    raise NotFoundError("staff_not_found")
                taken = await conn.fetchval(
                    "SELECT 1 FROM users WHERE staff_id = $1 AND id <> $2",
                    staff_id,
                    user_id,
                )
                if taken is not None:
                    raise LinkedError("staff_already_linked")
                await conn.execute(
                    "UPDATE users SET staff_id = $2, updated_at = now() WHERE id = $1",
                    user_id,
                    staff_id,
                )
            row = await conn.fetchrow(
                "SELECT u.id, u.username, u.display_name, u.role, u.is_disabled,"
                " u.staff_id, s.staff_code, u.created_at, u.updated_at"
                " FROM users u LEFT JOIN staff s ON s.id = u.staff_id WHERE u.id = $1",
                user_id,
            )
            return _user_row(row) if row else None


async def list_unlinked_staff(pool: asyncpg.Pool) -> list[dict]:
    """Staff rows with no linked login account (user-form picker source)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, staff_code, full_name FROM staff"
            " WHERE id NOT IN (SELECT staff_id FROM users WHERE staff_id IS NOT NULL)"
            " ORDER BY staff_code ASC"
        )
        out = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])
            out.append(d)
        return out


# --- Numbered (offset) pages: same newest-first order, additive ---

async def list_staff_offset(
    pool: asyncpg.Pool, *, page: int, page_size: int, q: str | None = None
) -> dict:
    """Numbered page (1-based) of the staff roster (q matches code / name)."""
    from .paging import offset_payload

    term = q.strip() if q and q.strip() else None
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM staff s"
            " WHERE ($1::text IS NULL OR (s.staff_code ILIKE '%' || $1 || '%'"
            "   OR s.full_name ILIKE '%' || $1 || '%'))",
            term,
        )
        rows = await conn.fetch(
            "SELECT s.id, s.staff_code, s.full_name, s.store_id, st.code AS store_code,"
            " s.role, s.brand_id, b.code AS brand_code,"
            " s.joined_at, s.left_at, s.created_at, s.updated_at"
            " FROM staff s LEFT JOIN stores st ON st.id = s.store_id"
            " LEFT JOIN brands b ON b.id = s.brand_id"
            " WHERE ($3::text IS NULL OR (s.staff_code ILIKE '%' || $3 || '%'"
            "   OR s.full_name ILIKE '%' || $3 || '%'))"
            " ORDER BY s.created_at DESC, s.id DESC LIMIT $1 OFFSET $2",
            page_size,
            (page - 1) * page_size,
            term,
        )
        items = [_staff_row(r) for r in rows]
    return offset_payload(items, int(total or 0), page, page_size)


async def list_users_offset(
    pool: asyncpg.Pool, *, page: int, page_size: int, q: str | None = None
) -> dict:
    """Numbered page (1-based) of login-account records (q matches username /
    display_name)."""
    from .paging import offset_payload

    term = q.strip() if q and q.strip() else None
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM users u"
            " WHERE ($1::text IS NULL OR (u.username ILIKE '%' || $1 || '%'"
            "   OR u.display_name ILIKE '%' || $1 || '%'))",
            term,
        )
        rows = await conn.fetch(
            "SELECT u.id, u.username, u.display_name, u.role, u.is_disabled,"
            " u.staff_id, s.staff_code, u.created_at, u.updated_at"
            " FROM users u LEFT JOIN staff s ON s.id = u.staff_id"
            " WHERE ($3::text IS NULL OR (u.username ILIKE '%' || $3 || '%'"
            "   OR u.display_name ILIKE '%' || $3 || '%'))"
            " ORDER BY u.created_at DESC, u.id DESC LIMIT $1 OFFSET $2",
            page_size,
            (page - 1) * page_size,
            term,
        )
        items = [_user_row(r) for r in rows]
    return offset_payload(items, int(total or 0), page, page_size)
