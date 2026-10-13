"""Group/brand repository: hierarchy CRUD + write-scope resolution (asyncpg).

Groups -> brands -> stores. Single-brand clients = one group + one brand.
DEFAULT_GROUP_CODE/DEFAULT_BRAND_CODE ('DEMO') is the holder for seed rows
and the fallback for writers with no brand link (e.g. HQ admin accounts and
pre-brand test sessions) so legacy creates keep working post-migration.

resolve_write_brand / resolve_write_group: explicit body value wins (must
exist, else NotFoundError); else the session brand/group; else the DEMO
holder. Brand-less sa keeps legacy group-wide reads (see deps.is_group_wide
note) — new sa rows should always carry a brand.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg

DEFAULT_GROUP_CODE = "DEMO"
DEFAULT_BRAND_CODE = "DEMO"


class DuplicateCodeError(Exception):
    pass


class NotFoundError(Exception):
    pass


def _row(r) -> dict:  # type: ignore[no-untyped-def]
    d = dict(r)
    d["id"] = str(d["id"])
    if d.get("group_id") is not None:
        d["group_id"] = str(d["group_id"])
    return d


# --- groups ---

async def create_group(pool: asyncpg.Pool, *, code: str, name: str) -> dict:
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO groups (code, name) VALUES ($1, $2)"
                " RETURNING id, code, name, created_at",
                code.strip(),
                name.strip(),
            )
        except asyncpg.UniqueViolationError as exc:
            raise DuplicateCodeError(str(exc))
        return _row(row)


async def list_groups(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT g.id, g.code, g.name, g.created_at,"
            " (SELECT COUNT(*) FROM brands b WHERE b.group_id = g.id) AS brand_count"
            " FROM groups g ORDER BY g.created_at, g.code"
        )
        return [_row(r) for r in rows]


async def list_groups_scoped(
    pool: asyncpg.Pool, *, group_id: UUID | None
) -> list[dict]:
    """Group silo: brand/manager sessions see only their own group; a
    brand-less admin (HQ, no staff link) sees every group."""
    if group_id is None:
        return await list_groups(pool)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT g.id, g.code, g.name, g.created_at,"
            " (SELECT COUNT(*) FROM brands b WHERE b.group_id = g.id) AS brand_count"
            " FROM groups g WHERE g.id = $1",
            group_id,
        )
        return [_row(r) for r in rows]


# --- brands ---

async def create_brand(
    pool: asyncpg.Pool, *, group_id: UUID, code: str, name: str
) -> dict:
    async with pool.acquire() as conn:
        ok = await conn.fetchval("SELECT 1 FROM groups WHERE id = $1", group_id)
        if ok is None:
            raise NotFoundError("group_not_found")
        try:
            row = await conn.fetchrow(
                "INSERT INTO brands (group_id, code, name)"
                " VALUES ($1, $2, $3)"
                " RETURNING id, group_id, code, name, created_at",
                group_id,
                code.strip(),
                name.strip(),
            )
        except asyncpg.UniqueViolationError as exc:
            raise DuplicateCodeError(str(exc))
        return _row(row)


async def get_brand(pool: asyncpg.Pool, brand_id: UUID) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT b.id, b.group_id, b.code, b.name, b.created_at,"
            " g.code AS group_code, g.name AS group_name,"
            " (SELECT COUNT(*) FROM stores s WHERE s.brand_id = b.id) AS store_count"
            " FROM brands b JOIN groups g ON g.id = b.group_id"
            " WHERE b.id = $1",
            brand_id,
        )
        return _row(row) if row is not None else None


async def list_brands(
    pool: asyncpg.Pool, *, group_id: UUID | None = None
) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT b.id, b.group_id, b.code, b.name, b.created_at,"
            " g.code AS group_code,"
            " (SELECT COUNT(*) FROM stores s WHERE s.brand_id = b.id) AS store_count"
            " FROM brands b JOIN groups g ON g.id = b.group_id"
            " WHERE ($1::uuid IS NULL OR b.group_id = $1::uuid)"
            " ORDER BY b.created_at, b.code",
            group_id,
        )
        return [_row(r) for r in rows]


async def default_brand_id(pool: asyncpg.Pool) -> UUID | None:
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT id FROM brands WHERE code = $1", DEFAULT_BRAND_CODE
        )
        return UUID(str(row)) if row is not None else None


async def default_group_id(pool: asyncpg.Pool) -> UUID | None:
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT id FROM groups WHERE code = $1", DEFAULT_GROUP_CODE
        )
        return UUID(str(row)) if row is not None else None


async def resolve_write_brand(
    pool: asyncpg.Pool,
    *,
    explicit: UUID | None,
    session_brand_id: UUID | None,
) -> UUID:
    """Brand for a new brand-scoped row. Explicit wins (must exist); else the
    session brand; else the DEMO holder. Raises NotFoundError / LookupError
    (no holder yet — fresh DB without migrations) instead of writing NULL."""
    async with pool.acquire() as conn:
        if explicit is not None:
            ok = await conn.fetchval(
                "SELECT 1 FROM brands WHERE id = $1", explicit
            )
            if ok is None:
                raise NotFoundError("brand_not_found")
            return explicit
        if session_brand_id is not None:
            return session_brand_id
        holder = await conn.fetchval(
            "SELECT id FROM brands WHERE code = $1", DEFAULT_BRAND_CODE
        )
        if holder is None:
            raise LookupError("no_default_brand")
        return UUID(str(holder))


async def group_of_brand(pool: asyncpg.Pool, brand_id: UUID) -> UUID | None:
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT group_id FROM brands WHERE id = $1", brand_id
        )
        return UUID(str(row)) if row is not None else None
