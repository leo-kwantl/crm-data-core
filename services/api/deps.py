"""Shared FastAPI dependencies: pool, session auth, rate-limit, log filter.

PII rules enforced here:
- No request-body logging on /customers/search (NoBodyLoggingFilter).
- Identity = session token in the `x-session-token` header (password login in
  routers/auth.py; admin-provisioned accounts, no SSO, no self-signup).
  The old x-staff-id stub is GONE (clean cutover) — every guarded route uses
  get_current_user; only POST /auth/login and GET /staff (roster) are public.
- Search rate-limit helper (in-memory fixed window; replace with Redis later),
  keyed by the session's staff_code.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import Depends, Header, HTTPException, Request

_pool: asyncpg.Pool | None = None

# In-memory fixed-window counters: {(staff_code, route): [window_start, count]}
_rate_counters: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0])


async def get_pool() -> AsyncIterator[asyncpg.Pool]:
    """Yield the shared asyncpg pool (created from DATABASE_URL)."""
    global _pool
    if _pool is None:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            from .db import get_database_url

            dsn = get_database_url()
        _pool = await asyncpg.create_pool(dsn)
    yield _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@dataclass
class CurrentUser:
    """Live session identity. Role = users.role (staff.role is roster data).

    Brand scope: staff/brand link (single brand per staff row; FIRST linked
    brand wins — multi-brand staff links deferred). brand_id None =
    group-wide, allowed for manager/admin roles only by convention (brand-less
    sa keeps legacy group-wide reads so pre-brand deployments keep working).
    Manager/admin see their whole group; brand-scoped sa sees only rows in
    their brand (cross-brand reads are 403 forbidden).

    NOTE (deferred): SA store-scoping is NOT implemented — the brand is the
    implemented scoping line; only admin-only mutations (user admin, password
    resets, staff create/delete) are role-gated via require_admin.
    """

    user_id: UUID
    username: str
    staff_code: str
    role: str
    brand_id: UUID | None = None
    brand_code: str | None = None
    brand_name: str | None = None
    group_id: UUID | None = None
    group_code: str | None = None
    group_name: str | None = None


def is_group_wide(user: CurrentUser) -> bool:
    """Whole-group visibility: manager/admin see every brand in their group."""
    return user.role in ("admin", "manager")


def scope_filters(user: CurrentUser) -> tuple:
    """(brand filter, group filter) for analytics list/compute queries.

    Delegates to analytics.scope.session_scope (single source of truth;
    see that module for the visibility rule). Brand-scoped staff see
    exactly their brand (group element None); group-wide roles see their
    group (brand element None, group element = session group; brand-less
    HQ sessions pass (None, None) = global)."""
    from .analytics.scope import session_scope

    return session_scope(user)


def check_brand(user: CurrentUser, brand_id: UUID | str | None) -> None:
    """Raise 403 forbidden when a brand-scoped session touches another brand.

    Group-wide roles (manager/admin) always pass; brand-less sessions pass
    (legacy/HQ — callers additionally silo by group where it matters).
    NULL-brand (legacy/pre-brand) rows pass: they predate scoping and are
    readable group-wide, writable only via group-wide roles. Use
    check_brand_strict for writes that must land inside a brand.
    """
    if is_group_wide(user) or user.brand_id is None or brand_id is None:
        return
    if str(brand_id) != str(user.brand_id):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})


async def require_customer_in_scope(pool, customer_id, user: CurrentUser,
                                      *, brand_id=None) -> None:
    """404 when the customer lives outside the caller's silo.

    Group check first (never leak cross-group existence); when brand_id is
    given (brand-scoped sessions), the customer must additionally have at
    least one completed order in that brand. Brand-less HQ sessions
    (group None, brand None) pass. Authenticated per-customer routers
    (events, orders, consents, tier assign) call this so UUID-keyed history
    reads cannot cross the silo. (The unauthenticated profile CRUD router is
    pre-existing no-auth design and stays untouched — see report.)
    """
    if user.group_id is None and brand_id is None:
        return
    from .repos import customers as customers_repo

    if user.group_id is not None:
        group = await customers_repo.group_of_customer(pool, customer_id)
        if group is not None and str(group) != str(user.group_id):
            raise HTTPException(status_code=404,
                                detail={"code": "customer_not_found"})
    if brand_id is not None:
        async with pool.acquire() as conn:
            seen = await conn.fetchval(
                "SELECT 1 FROM orders o JOIN stores s ON s.id = o.store_id"
                " WHERE o.customer_id = $1 AND o.status = 'completed'"
                " AND s.brand_id = $2::uuid LIMIT 1",
                customer_id, brand_id,
            )
        if not seen:
            raise HTTPException(status_code=404,
                                detail={"code": "customer_not_found"})


_SESSION_ERRORS = {
    "missing": (401, "missing_session"),
    "unknown": (401, "invalid_session"),
    "expired": (401, "expired_session"),
    "disabled": (401, "login_disabled"),
}


async def get_current_user(
    x_session_token: Annotated[str | None, Header(alias="x-session-token")] = None,
    pool: asyncpg.Pool = Depends(get_pool),  # noqa: B008
) -> CurrentUser:
    """Session auth. 401 missing/invalid/expired/disabled (distinct codes)."""
    from .repos import auth as auth_repo

    if not x_session_token or not x_session_token.strip():
        raise HTTPException(status_code=401, detail={"code": "missing_session"})
    result = await auth_repo.get_session_user(pool, x_session_token.strip())
    if isinstance(result, str):
        status, code = _SESSION_ERRORS[result]
        raise HTTPException(status_code=status, detail={"code": code})
    return CurrentUser(
        user_id=UUID(result["user_id"]),
        username=result["username"],
        staff_code=result["staff_code"],
        role=result["role"],
        brand_id=UUID(result["brand_id"]) if result.get("brand_id") else None,
        brand_code=result.get("brand_code"),
        brand_name=result.get("brand_name"),
        group_id=UUID(result["group_id"]) if result.get("group_id") else None,
        group_code=result.get("group_code"),
        group_name=result.get("group_name"),
    )


async def get_session_token(
    x_session_token: Annotated[str | None, Header(alias="x-session-token")] = None,
) -> str | None:
    """Raw bearer token (for logout / session revocation), None when absent."""
    if x_session_token and x_session_token.strip():
        return x_session_token.strip()
    return None


async def require_admin(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    """Admin-only guard: user/staff administration + password resets. 403 otherwise."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    return user


def check_search_rate_limit(
    staff_code: str, *, limit: int = 60, window_s: int = 60, route: str = "search"
) -> None:
    """Fixed-window rate limit per session staff_code. Raises 429 when exceeded."""
    now = time.monotonic()
    key = (staff_code, route)
    start, count = _rate_counters[key]
    if now - start > window_s:
        _rate_counters[key] = [now, 1]
        return
    if count >= limit:
        raise HTTPException(status_code=429, detail={"code": "rate_limited"})
    _rate_counters[key][1] += 1


def reset_rate_limits() -> None:
    _rate_counters.clear()


class NoBodyLoggingFilter(logging.Filter):
    """Drop log records carrying request bodies for PII routes.

    Attach to uvicorn access loggers; any record whose message mentions
    /customers/search with a body payload is suppressed. Route handlers must
    also never log search bodies explicitly.
    """

    PII_PATHS = ("/customers/search",)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(p in msg and "body" in msg.lower() for p in self.PII_PATHS)


async def no_store_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Middleware: Cache-Control: no-store on PII + identity + auth routes."""
    response = await call_next(request)
    if (
        request.url.path.startswith("/api/v1/customers")
        or request.url.path.startswith("/api/v1/orders")
        or request.url.path.startswith("/api/v1/staff")
        or request.url.path.startswith("/api/v1/users")
        or request.url.path.startswith("/api/v1/auth")
    ):
        response.headers["Cache-Control"] = "no-store"
    return response
