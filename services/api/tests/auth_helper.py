"""Shared session-auth helper for the test suite (replaces x-staff-id).

Existing test modules keep their own header constants — point them at
AUTH_H (one line each) and the conftest autouse fixture seeds that admin
session before every test. Modules needing distinct identities (role-403s,
login flows) use make_session() with their own pool fixture.
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "migrations"

ADMIN_TOKEN = "test-admin-session-token"
ADMIN_HEADERS = {"x-session-token": ADMIN_TOKEN}
ADMIN_STAFF_CODE = "S-014"
ADMIN_USERNAME = "test.admin"
AUTH_H = ADMIN_HEADERS


def _token_sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _ensure_schema(conn: asyncpg.Connection) -> None:
    """Run any missing migrations (tolerates already-applied ones)."""
    import re

    if await conn.fetchval("SELECT to_regclass('public.sessions')") is not None:
        return
    up_re = re.compile(r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)", re.S | re.I)
    for mig in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = up_re.search(mig.read_text(encoding="utf-8"))
        if m and m.group(1).strip():
            try:
                await conn.execute(m.group(1).strip())
            except Exception as exc:
                if "already exists" not in str(exc).lower():
                    raise


async def ensure_admin_session(pool: asyncpg.Pool) -> dict[str, str]:
    """Upsert the shared admin identity + session. Returns AUTH_H."""
    return await make_session(
        pool,
        "admin",
        role="admin",
        username=ADMIN_USERNAME,
        staff_code=ADMIN_STAFF_CODE,
        token=ADMIN_TOKEN,
    )


async def make_session(
    pool: asyncpg.Pool,
    tag: str,
    *,
    role: str = "admin",
    username: str | None = None,
    staff_code: str | None = None,
    token: str | None = None,
    password: str | None = None,
    disabled: bool = False,
    brand_id: str | None = None,
) -> dict[str, str]:
    """Upsert user (tag, role) + session row; return its header dict.

    Defaults give an admin-capable session. staff_code defaults to
    f"TEST-{tag}" so audit-trail assertions can predict it. When password is
    given the account can also log in for real via POST /auth/login.
    """
    import bcrypt

    username = username or f"test-{tag}"
    staff_code = staff_code or f"TEST-{tag}"
    token = token or f"test-token-{tag}-{secrets.token_hex(4)}"
    async with pool.acquire() as conn:
        await _ensure_schema(conn)
        pw_hash = (
            bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            if password
            else None
        )
        await conn.execute(
            "INSERT INTO users (username, display_name, role, is_disabled, password_hash)"
            " VALUES ($1, $2, $3, $4, $5)"
            " ON CONFLICT (username) DO UPDATE SET role = $3, is_disabled = $4,"
            " password_hash = COALESCE($5, users.password_hash)",
            username,
            f"Test {tag}",
            role,
            disabled,
            pw_hash,
        )
        user_id = await conn.fetchval(
            "SELECT id FROM users WHERE username = $1", username
        )
        if brand_id is not None:
            # Brand-scoped identity: link the login to a staff row carrying
            # the brand (single brand per staff row; FIRST linked brand wins).
            await conn.execute(
                "INSERT INTO staff (staff_code, full_name, role, brand_id)"
                " VALUES ($1, $2, $3, $4)"
                " ON CONFLICT (staff_code) DO UPDATE"
                " SET role = $3, brand_id = $4",
                staff_code,
                f"Test {tag}",
                role,
                brand_id,
            )
            staff_id = await conn.fetchval(
                "SELECT id FROM staff WHERE staff_code = $1", staff_code
            )
            await conn.execute(
                "UPDATE users SET staff_id = $2 WHERE id = $1",
                user_id,
                staff_id,
            )
        await conn.execute(
            "INSERT INTO sessions (token_sha256, user_id, staff_code, expires_at)"
            " VALUES ($1, $2, $3, now() + INTERVAL '30 days')"
            " ON CONFLICT (token_sha256) DO UPDATE"
            " SET user_id = $2, staff_code = $3, expires_at = now() + INTERVAL '30 days'",
            _token_sha(token),
            user_id,
            staff_code,
        )
    return {"x-session-token": token}
