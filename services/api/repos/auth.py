"""Password + session repository (raw SQL, asyncpg).

Sessions authenticate USERS (sessions.user_id). The effective role is
users.role; staff.role stays roster/HR data only. staff_code recorded on the
session is the linked staff row's code, falling back to the username when the
account has no staff link (e.g. the HQ admin).

Only SHA-256 hex of bearer tokens is stored — raw tokens never touch the DB.
SA store-scoping is NOT implemented (deferred); all signed-in roles see all
data, only the admin-only mutations below are role-gated.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg

SESSION_TTL = timedelta(hours=12)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    import bcrypt

    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(password: str, password_hash: str) -> bool:
    import bcrypt

    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


async def find_login_identity(pool: asyncpg.Pool, identifier: str) -> dict | None:
    """User row for login by username OR linked staff_code (case-sensitive).

    Returns None when unknown. Includes password_hash (never serialized).
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT u.id, u.username, u.display_name, u.role, u.is_disabled,"
            " u.staff_id, u.password_hash, s.staff_code"
            " FROM users u LEFT JOIN staff s ON s.id = u.staff_id"
            " WHERE u.username = $1 OR s.staff_code = $1",
            identifier.strip(),
        )
        if row is None:
            return None
        d = dict(row)
        d["id"] = str(d["id"])
        if d.get("staff_id") is not None:
            d["staff_id"] = str(d["staff_id"])
        return d


def session_staff_code(identity: dict) -> str:
    return identity.get("staff_code") or identity["username"]


async def login_brand_context(pool: asyncpg.Pool, user_id: UUID) -> dict:
    """Brand/group context for the login + me responses (staff link only)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT st.brand_id, b.code AS brand_code, b.name AS brand_name,"
            " b.group_id, g.code AS group_code, g.name AS group_name"
            " FROM users u LEFT JOIN staff st ON st.id = u.staff_id"
            " LEFT JOIN brands b ON b.id = st.brand_id"
            " LEFT JOIN groups g ON g.id = b.group_id"
            " WHERE u.id = $1",
            user_id,
        )
    if row is None:
        return {}
    d = dict(row)
    return {
        "brand_id": str(d["brand_id"]) if d.get("brand_id") else None,
        "brand_code": d.get("brand_code"),
        "brand_name": d.get("brand_name"),
        "group_id": str(d["group_id"]) if d.get("group_id") else None,
        "group_code": d.get("group_code"),
        "group_name": d.get("group_name"),
    }


async def create_session(
    pool: asyncpg.Pool, *, user_id: UUID, staff_code: str
) -> tuple[str, datetime]:
    """Mint a session, pruning expired rows first. Returns (raw_token, expires_at)."""
    import secrets

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM sessions WHERE expires_at < now()")
        await conn.execute(
            "INSERT INTO sessions (token_sha256, user_id, staff_code, expires_at)"
            " VALUES ($1, $2, $3, $4)",
            hash_token(token),
            user_id,
            staff_code,
            expires_at,
        )
    return token, expires_at


async def get_session_user(pool: asyncpg.Pool, token: str) -> dict | str:
    """Resolve a bearer token to its live identity dict.

    Brand/group context rides along: the linked staff row's brand (single
    brand per staff row — a staffer linked to several brands uses the FIRST
    linked brand; multi-brand staff links are a deferred follow-up) and its
    group. Brand-less sessions (HQ admin, pre-brand test accounts) carry
    None brand/group and are treated as group-wide by deps.is_group_wide
    only when the role is admin/manager.

    Returns "missing" (no header), "unknown" (no such token), "expired", or
    "disabled" instead of raising, so the router maps each to its 401 code.
    """
    if not token:
        return "missing"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT s.user_id, s.staff_code, s.expires_at,"
            " u.username, u.role, u.is_disabled, u.staff_id,"
            " st.brand_id, b.code AS brand_code, b.name AS brand_name,"
            " b.group_id, g.code AS group_code, g.name AS group_name"
            " FROM sessions s JOIN users u ON u.id = s.user_id"
            " LEFT JOIN staff st ON st.id = u.staff_id"
            " LEFT JOIN brands b ON b.id = st.brand_id"
            " LEFT JOIN groups g ON g.id = b.group_id"
            " WHERE s.token_sha256 = $1",
            hash_token(token),
        )
    if row is None:
        return "unknown"
    d = dict(row)
    if d["is_disabled"]:
        return "disabled"
    if d["expires_at"] < datetime.now(timezone.utc):
        return "expired"
    d["user_id"] = str(d["user_id"])
    for key in ("staff_id", "brand_id", "group_id"):
        if d.get(key) is not None:
            d[key] = str(d[key])
    return d


async def revoke_session(pool: asyncpg.Pool, token: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM sessions WHERE token_sha256 = $1", hash_token(token)
        )


async def revoke_other_sessions(pool: asyncpg.Pool, *, user_id: UUID, token: str) -> None:
    """Drop every session for the user except the caller's (password change/reset)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM sessions WHERE user_id = $1 AND token_sha256 <> $2",
            user_id,
            hash_token(token),
        )


async def set_password_hash(pool: asyncpg.Pool, user_id: UUID, password: str) -> bool:
    """Set a user's bcrypt hash. Returns False when the user does not exist."""
    async with pool.acquire() as conn:
        cur = await conn.execute(
            "UPDATE users SET password_hash = $2, updated_at = now() WHERE id = $1",
            user_id,
            hash_password(password),
        )
        return cur.split()[-1] != "0"
