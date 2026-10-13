"""Membership-tier repository: tier CRUD + manual assignment + loyalty rollup.

Writes touch membership_tiers and customers.tier_code ONLY — no other
customer columns are modified here (customer profile ownership stays with
the customers repo). Loyalty stats are derived live from orders.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg


class DuplicateCodeError(Exception):
    pass


def _norm(code: str) -> str:
    return code.strip().upper()


def _tier_row(r) -> dict:  # type: ignore[no-untyped-def]
    d = dict(r)
    d["id"] = str(d["id"])
    if d.get("brand_id") is not None:
        d["brand_id"] = str(d["brand_id"])
    return d


async def list_tiers(
    pool: asyncpg.Pool, *, brand_id: UUID | None = None
) -> list[dict]:
    """Global (NULL-brand) catalogue rows + the session brand's own tiers.

    brand_id None (group-wide roles) returns every tier. pool_scope stays a
    pure reservation — no logic reads it (see migration 011 comment)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT t.id, t.code, t.name, t.rank, t.benefits_text, t.created_at,"
            " t.brand_id, b.code AS brand_code"
            " FROM membership_tiers t LEFT JOIN brands b ON b.id = t.brand_id"
            " WHERE ($1::uuid IS NULL"
            "   OR t.brand_id IS NULL OR t.brand_id = $1::uuid)"
            " ORDER BY t.rank ASC, t.code ASC",
            brand_id,
        )
        return [_tier_row(r) for r in rows]


async def get_tier(pool: asyncpg.Pool, code: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, code, name, rank, benefits_text, created_at, brand_id"
            " FROM membership_tiers WHERE code = $1",
            _norm(code),
        )
        return _tier_row(row) if row is not None else None


async def create_tier(
    pool: asyncpg.Pool,
    *,
    code: str,
    name: str,
    rank: int,
    benefits_text: str | None,
    brand_id: UUID | None = None,
) -> dict:
    """brand_id None = global catalogue row (visible to every brand)."""
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO membership_tiers"
                " (code, name, rank, benefits_text, brand_id)"
                " VALUES ($1, $2, $3, $4, $5)"
                " RETURNING id, code, name, rank, benefits_text, created_at, brand_id",
                _norm(code),
                name.strip(),
                rank,
                benefits_text,
                brand_id,
            )
        except asyncpg.UniqueViolationError as exc:
            raise DuplicateCodeError(str(exc))
        return _tier_row(row)


async def update_tier(
    pool: asyncpg.Pool,
    code: str,
    *,
    name: str | None,
    rank: int | None,
    benefits_text: str | None,
    clear_benefits: bool,
) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT code FROM membership_tiers WHERE code = $1", _norm(code)
        )
        if row is None:
            return None
        if name is not None:
            await conn.execute(
                "UPDATE membership_tiers SET name = $2 WHERE code = $1",
                _norm(code),
                name.strip(),
            )
        if rank is not None:
            await conn.execute(
                "UPDATE membership_tiers SET rank = $2 WHERE code = $1",
                _norm(code),
                rank,
            )
        if clear_benefits:
            await conn.execute(
                "UPDATE membership_tiers SET benefits_text = NULL WHERE code = $1",
                _norm(code),
            )
        elif benefits_text is not None:
            await conn.execute(
                "UPDATE membership_tiers SET benefits_text = $2 WHERE code = $1",
                _norm(code),
                benefits_text,
            )
        updated = await conn.fetchrow(
            "SELECT id, code, name, rank, benefits_text, created_at"
            " FROM membership_tiers WHERE code = $1",
            _norm(code),
        )
        return _tier_row(updated)


async def assign_tier(
    pool: asyncpg.Pool, customer_id: UUID, tier_code: str | None
) -> dict | tuple[None, str] | None:
    """Set/clear customers.tier_code. Returns the assignment row, None when
    the customer is missing, or (None, error_code) for a bad tier code."""
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM customers WHERE id = $1 AND deleted_at IS NULL",
            customer_id,
        )
        if exists is None:
            return None
        norm = _norm(tier_code) if tier_code is not None else None
        if norm is not None:
            ok = await conn.fetchval(
                "SELECT 1 FROM membership_tiers WHERE code = $1", norm
            )
            if ok is None:
                return (None, "tier_not_found")
        await conn.execute(
            "UPDATE customers SET tier_code = $2, updated_at = now() WHERE id = $1",
            customer_id,
            norm,
        )
        row = await conn.fetchrow(
            "SELECT c.id, c.member_id, c.tier_code,"
            " t.id AS tid, t.code, t.name, t.rank, t.benefits_text, t.created_at"
            " FROM customers c LEFT JOIN membership_tiers t ON t.code = c.tier_code"
            " WHERE c.id = $1",
            customer_id,
        )
        d = dict(row)
        d["id"] = str(d["id"])
        if d.get("tid") is not None:
            d["tier"] = {
                "id": str(d.pop("tid")),
                "code": d.pop("code"),
                "name": d.pop("name"),
                "rank": d.pop("rank"),
                "benefits_text": d.pop("benefits_text"),
                "created_at": d.pop("created_at"),
            }
        else:
            for k in ("tid", "code", "name", "rank", "benefits_text", "created_at"):
                d.pop(k, None)
            d["tier"] = None
        return d


async def get_loyalty(pool: asyncpg.Pool, customer_id: UUID) -> dict | None:
    """Tier + live order rollup. Read-only (SELECT on customers/orders/tiers)."""
    async with pool.acquire() as conn:
        cust = await conn.fetchrow(
            "SELECT c.id, c.member_id, c.tier_code,"
            " t.id AS tid, t.code, t.name, t.rank, t.benefits_text, t.created_at,"
            " (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS order_count,"
            " (SELECT COALESCE(SUM(o.total_cents), 0) FROM orders o"
            "   WHERE o.customer_id = c.id) AS total_spent_cents"
            " FROM customers c LEFT JOIN membership_tiers t ON t.code = c.tier_code"
            " WHERE c.id = $1 AND c.deleted_at IS NULL",
            customer_id,
        )
        if cust is None:
            return None
        d = dict(cust)
        out = {
            "customer_id": str(d["id"]),
            "member_id": d["member_id"],
            "order_count": int(d["order_count"]),
            "total_spent_cents": int(d["total_spent_cents"]),
        }
        if d.get("tid") is not None:
            out["tier"] = {
                "id": str(d["tid"]),
                "code": d["code"],
                "name": d["name"],
                "rank": d["rank"],
                "benefits_text": d["benefits_text"],
                "created_at": d["created_at"],
            }
        else:
            out["tier"] = None
        return out
