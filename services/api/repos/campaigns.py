"""Campaign repository: CRUD + status machine + member assignments.

Raw SQL via asyncpg, no ORM. Member-list cursor = base64url JSON
{"at": <assigned_at iso>, "id": <customer_id>} keying
(assigned_at DESC, customer_id DESC) within one campaign.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from . import reference as refrepo
from .reference import UnknownCodeError


class DuplicateCodeError(Exception):
    pass


class IllegalTransitionError(Exception):
    """Status jump outside draft -> active -> ended."""


class EndedError(Exception):
    """Member change attempted on an ended campaign."""


class DuplicateMemberError(Exception):
    pass


_ALLOWED = {"draft": ("active",), "active": ("ended",), "ended": ()}
"""Fallback machine, used only when the campaign_status reference rows are
unavailable. Normally transitions derive from reference sort_rank order."""


def check_transition(current: str, nxt: str, *, order: list[str] | None = None) -> None:
    """Status machine. With `order` (active campaign_status codes ranked by
    sort_rank) the legal moves are same-rank (no-op PATCH) or next-rank-up,
    so draft -> active -> ended is driven by reference rows. Without `order`
    falls back to the hardcoded _ALLOWED map."""
    if nxt == current:
        return  # no-op PATCH, allowed
    if order:
        try:
            cur_i, nxt_i = order.index(current), order.index(nxt)
        except ValueError:
            raise IllegalTransitionError(f"{current}->{nxt}")
        if nxt_i == cur_i + 1:
            return
        raise IllegalTransitionError(f"{current}->{nxt}")
    if nxt not in _ALLOWED.get(current, ()):
        raise IllegalTransitionError(f"{current}->{nxt}")


def encode_cursor(at: datetime, customer_id: UUID) -> str:
    raw = json.dumps({"at": at.isoformat(), "id": str(customer_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
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
    if isinstance(d.get("config"), str):
        d["config"] = json.loads(d["config"])
    return d


async def create_campaign(
    pool: asyncpg.Pool,
    *,
    code: str,
    name: str,
    kind: str,
    starts_at: datetime | None,
    ends_at: datetime | None,
    config: dict[str, Any],
    brand_id: UUID,
) -> dict:
    async with pool.acquire() as conn:
        kind_code = (kind or "promotion").strip()
        await refrepo.require_active(conn, "campaign_kind", kind_code)
        try:
            row = await conn.fetchrow(
                "INSERT INTO campaigns"
                " (code, name, kind, starts_at, ends_at, config, brand_id)"
                " VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)"
                " RETURNING id, code, name, kind, status, starts_at, ends_at,"
                " config, brand_id, created_at, updated_at",
                code.strip(),
                name.strip(),
                kind_code,
                starts_at,
                ends_at,
                json.dumps(config or {}),
                brand_id,
            )
        except asyncpg.UniqueViolationError as exc:
            raise DuplicateCodeError(str(exc))
        return _row(row)


async def get_campaign(pool: asyncpg.Pool, campaign_id: UUID) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT c.id, c.code, c.name, c.kind, c.status, c.starts_at, c.ends_at,"
            " c.config, c.created_at, c.updated_at,"
            " c.brand_id, b.code AS brand_code, b.group_id,"
            " (SELECT COUNT(*) FROM campaign_members m WHERE m.campaign_id = c.id)"
            " AS member_count FROM campaigns c"
            " LEFT JOIN brands b ON b.id = c.brand_id WHERE c.id = $1",
            campaign_id,
        )
        return _row(row) if row is not None else None


async def list_campaigns_page(
    pool: asyncpg.Pool,
    *,
    limit: int,
    cursor: str | None,
    brand_id: UUID | None = None,
    group_id: UUID | None = None,
    q: str | None = None,
) -> tuple[list[dict], str | None]:
    """Newest-first page (brand/group scoped, q matches code / name)."""
    from .stores import decode_cursor as _dec
    from .stores import encode_cursor as _enc

    cursor_at: datetime | None = None
    cursor_id: UUID | None = None
    if cursor:
        cursor_at, cursor_id = _dec(cursor)
    term = q.strip() if q and q.strip() else None
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.id, c.code, c.name, c.kind, c.status, c.starts_at, c.ends_at,"
            " c.config, c.created_at, c.updated_at, c.brand_id, b.code AS brand_code"
            " FROM campaigns c LEFT JOIN brands b ON b.id = c.brand_id"
            " WHERE ($1::timestamptz IS NULL"
            "   OR (c.created_at, c.id) < ($1::timestamptz, $2::uuid))"
            "   AND ($4::uuid IS NULL OR c.brand_id = $4::uuid)"
            "   AND ($5::uuid IS NULL OR b.group_id = $5::uuid)"
            "   AND ($6::text IS NULL OR (c.code ILIKE '%' || $6 || '%'"
            "     OR c.name ILIKE '%' || $6 || '%'))"
            " ORDER BY c.created_at DESC, c.id DESC LIMIT $3",
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
        result.pop()
        last = result[-1]
        next_cursor = _enc(last["created_at"], UUID(str(last["id"])))
    return result, next_cursor


async def list_campaigns_offset(
    pool: asyncpg.Pool,
    *,
    page: int,
    page_size: int,
    brand_id: UUID | None = None,
    group_id: UUID | None = None,
    q: str | None = None,
) -> dict:
    """Numbered page (1-based) of campaigns, newest-first (brand/group scoped,
    q matches code / name)."""
    from .paging import offset_payload

    term = q.strip() if q and q.strip() else None
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM campaigns c"
            " LEFT JOIN brands b ON b.id = c.brand_id"
            " WHERE ($1::uuid IS NULL OR c.brand_id = $1::uuid)"
            "   AND ($2::uuid IS NULL OR b.group_id = $2::uuid)"
            "   AND ($3::text IS NULL OR (c.code ILIKE '%' || $3 || '%'"
            "     OR c.name ILIKE '%' || $3 || '%'))",
            brand_id,
            group_id,
            term,
        )
        rows = await conn.fetch(
            "SELECT c.id, c.code, c.name, c.kind, c.status, c.starts_at, c.ends_at,"
            " c.config, c.created_at, c.updated_at, c.brand_id, b.code AS brand_code"
            " FROM campaigns c LEFT JOIN brands b ON b.id = c.brand_id"
            " WHERE ($3::uuid IS NULL OR c.brand_id = $3::uuid)"
            "   AND ($4::uuid IS NULL OR b.group_id = $4::uuid)"
            "   AND ($5::text IS NULL OR (c.code ILIKE '%' || $5 || '%'"
            "     OR c.name ILIKE '%' || $5 || '%'))"
            " ORDER BY c.created_at DESC, c.id DESC LIMIT $1 OFFSET $2",
            page_size,
            (page - 1) * page_size,
            brand_id,
            group_id,
            term,
        )
        items = [_row(r) for r in rows]
    return offset_payload(items, int(total or 0), page, page_size)


async def update_campaign(
    pool: asyncpg.Pool,
    campaign_id: UUID,
    *,
    name: str | None,
    status: str | None,
    starts_at: datetime | None,
    clear_starts_at: bool,
    ends_at: datetime | None,
    clear_ends_at: bool,
    config: dict[str, Any] | None,
) -> dict | None:
    async with pool.acquire() as conn:
        cur = await conn.fetchrow(
            "SELECT status FROM campaigns WHERE id = $1", campaign_id
        )
        if cur is None:
            return None
        if status is not None:
            order = await refrepo.ordered_codes(conn, "campaign_status")
            if order and status not in order:
                raise UnknownCodeError("campaign_status", status)
            check_transition(cur["status"], status, order=order or None)
            await conn.execute(
                "UPDATE campaigns SET status = $2, updated_at = now() WHERE id = $1",
                campaign_id,
                status,
            )
        if name is not None:
            await conn.execute(
                "UPDATE campaigns SET name = $2, updated_at = now() WHERE id = $1",
                campaign_id,
                name.strip(),
            )
        if clear_starts_at:
            await conn.execute(
                "UPDATE campaigns SET starts_at = NULL, updated_at = now()"
                " WHERE id = $1",
                campaign_id,
            )
        elif starts_at is not None:
            await conn.execute(
                "UPDATE campaigns SET starts_at = $2, updated_at = now()"
                " WHERE id = $1",
                campaign_id,
                starts_at,
            )
        if clear_ends_at:
            await conn.execute(
                "UPDATE campaigns SET ends_at = NULL, updated_at = now() WHERE id = $1",
                campaign_id,
            )
        elif ends_at is not None:
            await conn.execute(
                "UPDATE campaigns SET ends_at = $2, updated_at = now() WHERE id = $1",
                campaign_id,
                ends_at,
            )
        if config is not None:
            await conn.execute(
                "UPDATE campaigns SET config = $2::jsonb, updated_at = now()"
                " WHERE id = $1",
                campaign_id,
                json.dumps(config),
            )
        row = await conn.fetchrow(
            "SELECT c.id, c.code, c.name, c.kind, c.status, c.starts_at, c.ends_at,"
            " c.config, c.created_at, c.updated_at,"
            " c.brand_id, b.code AS brand_code, b.group_id,"
            " (SELECT COUNT(*) FROM campaign_members m WHERE m.campaign_id = c.id)"
            " AS member_count FROM campaigns c"
            " LEFT JOIN brands b ON b.id = c.brand_id WHERE c.id = $1",
            campaign_id,
        )
        return _row(row)


async def delete_campaign(pool: asyncpg.Pool, campaign_id: UUID) -> bool:
    """Delete + cascade member rows (ON DELETE CASCADE). Returns False if missing."""
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM campaigns WHERE id = $1", campaign_id)
        return not res.endswith("0")


async def add_member(
    pool: asyncpg.Pool,
    campaign_id: UUID,
    customer_id: UUID,
    *,
    assigned_by: str,
) -> dict | tuple[None, str]:
    """Returns member row, or (None, error_code): campaign_not_found,
    customer_not_found, campaign_ended, duplicate_member."""
    async with pool.acquire() as conn:
        camp = await conn.fetchrow(
            "SELECT status FROM campaigns WHERE id = $1", campaign_id
        )
        if camp is None:
            return (None, "campaign_not_found")
        if camp["status"] == "ended":
            return (None, "campaign_ended")
        cust = await conn.fetchrow(
            "SELECT member_id, full_name FROM customers"
            " WHERE id = $1 AND deleted_at IS NULL",
            customer_id,
        )
        if cust is None:
            return (None, "customer_not_found")
        try:
            row = await conn.fetchrow(
                "INSERT INTO campaign_members (campaign_id, customer_id, assigned_by)"
                " VALUES ($1, $2, $3)"
                " RETURNING customer_id, assigned_by, assigned_at",
                campaign_id,
                customer_id,
                assigned_by,
            )
        except asyncpg.UniqueViolationError:
            return (None, "duplicate_member")
        return {
            "customer_id": str(row["customer_id"]),
            "member_id": cust["member_id"],
            "full_name": cust["full_name"],
            "assigned_by": row["assigned_by"],
            "assigned_at": row["assigned_at"],
        }


async def remove_member(
    pool: asyncpg.Pool, campaign_id: UUID, customer_id: UUID
) -> bool:
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM campaign_members WHERE campaign_id = $1 AND customer_id = $2",
            campaign_id,
            customer_id,
        )
        return not res.endswith("0")


async def list_members_page(
    pool: asyncpg.Pool,
    campaign_id: UUID,
    *,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict] | None, str | None]:
    """Returns (rows, next_cursor), or (None, None) when campaign is missing."""
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM campaigns WHERE id = $1", campaign_id
        )
        if exists is None:
            return None, None
        cursor_at: datetime | None = None
        cursor_id: UUID | None = None
        if cursor:
            cursor_at, cursor_id = decode_cursor(cursor)
        rows = await conn.fetch(
            "SELECT m.customer_id, c.member_id, c.full_name,"
            " m.assigned_by, m.assigned_at"
            " FROM campaign_members m JOIN customers c ON c.id = m.customer_id"
            " WHERE m.campaign_id = $1"
            " AND ($2::timestamptz IS NULL"
            "   OR (m.assigned_at, m.customer_id)"
            "    < ($2::timestamptz, $3::uuid))"
            " ORDER BY m.assigned_at DESC, m.customer_id DESC LIMIT $4",
            campaign_id,
            cursor_at,
            cursor_id,
            limit + 1,
        )
    result = [
        {
            "customer_id": str(r["customer_id"]),
            "member_id": r["member_id"],
            "full_name": r["full_name"],
            "assigned_by": r["assigned_by"],
            "assigned_at": r["assigned_at"],
        }
        for r in rows
    ]
    next_cursor = None
    if len(result) > limit:
        result.pop()
        last = result[-1]
        next_cursor = encode_cursor(last["assigned_at"], UUID(str(last["customer_id"])))
    return result, next_cursor
