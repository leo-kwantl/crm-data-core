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
from uuid import UUID, uuid4

import asyncpg
from fastapi import Depends, Header, HTTPException, Request
from starlette.datastructures import MutableHeaders

_pool: asyncpg.Pool | None = None

# In-memory fixed-window counters: {(staff_code, route): [window_start, count]}
_rate_counters: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0])


async def get_pool() -> AsyncIterator[asyncpg.Pool]:
    """Yield the shared asyncpg pool (created from resolve_db_url())."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(resolve_db_url())
    yield _pool


def resolve_db_url() -> str:
    """The DSN this app instance serves: env DATABASE_URL, else the
    pgembed default. Routers pass this (not the raw env var) to
    out-of-process CLIs so the subprocess lands on the SAME database
    the request just wrote to — under pytest the pool is overridden to
    an ephemeral instance while the env var may be absent."""
    from .db import get_database_url

    return get_database_url()


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

    The ANALYTICS scope is NOT decided here: it is decided by the staff row's
    own shape in analytics.scope.session_scope (A114 -- the group is not a
    scope, and the role is not part of the rule). The brand/group fields
    below still drive the CUSTOMER reads (is_group_wide, session_brand).

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


async def get_current_user_optional(
    x_session_token: Annotated[str | None, Header(alias="x-session-token")] = None,
    pool: asyncpg.Pool = Depends(get_pool),  # noqa: B008
) -> CurrentUser | None:
    """Session auth where anonymous is a valid caller (the pre-login roster).

    ONLY a missing header means anonymous. A present-but-bad token still
    401s through get_current_user, so a broken session stays loud instead of
    silently degrading to the reduced public projection.
    """
    if not x_session_token or not x_session_token.strip():
        return None
    return await get_current_user(x_session_token=x_session_token, pool=pool)


def is_group_wide(user: CurrentUser) -> bool:
    """Whole-group visibility for CUSTOMER reads: manager/admin see every
    brand in their group.

    This is the customer-read rule (which customers a session may see), NOT
    the analytics scope: the analytics scope comes from the staff row's own
    shape (analytics.scope.session_scope, A114).
    """
    return user.role in ("admin", "manager")


def producer_of(user: CurrentUser) -> str:
    """The name a row's ``created_by`` carries when this session writes it.

    A served row whose producer is NULL says nobody wrote it, and the
    roster identity is not always there to name: a session can be a login
    without a staff row (the HQ admin), and ``staff_code`` is then NULL.
    So the producer is the staff code when the session has one and the
    login name when it does not -- and never NULL.
    """
    for candidate in (user.staff_code, user.username):
        name = (candidate or "").strip()
        if name:
            return name
    return "unknown"  # pragma: no cover - a session with no identity at all


def session_brand(user: CurrentUser) -> UUID | None:
    """The brand this session's customer reads are pinned to (None = not pinned).

    Single definition of the rule every read path uses: group-wide roles are
    never brand-pinned (they see their whole group); a brand-scoped session is
    pinned to its own brand. Passing this to a query or guard fails closed —
    forgetting to pass it is the 7.2-2 leak, so callers should not hand-roll
    `user.brand_id`.
    """
    return None if is_group_wide(user) else user.brand_id


def scope_filters(user: CurrentUser) -> tuple:
    """(brand filter, group filter) for analytics list/compute queries.

    Delegates to analytics.scope.session_scope (single source of truth;
    see that module for the visibility rule). A session linked to a brand
    reads exactly that brand (group element None); a session linked to
    neither a brand nor a group reads the global scope (None, None). The
    group is NOT a scope (A114): a session that carries a group and no
    brand has no analytics scope at all, and this funnel refuses it with
    403 {"code": "no_analytics_scope"} -- never a zero-filled payload that
    reads as "no activity"."""
    from .analytics.scope import no_scope_error, session_scope

    scope = session_scope(user)
    if not scope.has_scope:
        raise no_scope_error()
    return scope.filters


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
    reads cannot cross the silo. The profile CRUD router (customers.py)
    is authenticated and guards its {id} routes with this helper.

    Fail closed (7.2-2): a brand-scoped session is checked against ITS brand
    whether or not the caller passed one. Callers used to leave brand_id
    None, which made every per-customer route cross-brand readable. Passing
    brand_id explicitly still wins (analytics routers name a brand filter);
    group-wide roles are never brand-pinned — they see their whole group.

    "In this brand" is the same rule as the list predicates in
    repos/customers.py: a completed order in that brand, OR no completed
    order anywhere (a fresh lead / pre-brand row is unattributed, and the
    store that just created it must be able to read it back). A customer
    who has only ever bought in another brand is not in this one.
    """
    if brand_id is None and not is_group_wide(user):
        brand_id = user.brand_id
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
                "SELECT EXISTS (SELECT 1 FROM orders o"
                "                JOIN stores s ON s.id = o.store_id"
                "                WHERE o.customer_id = $1"
                "                  AND o.status = 'completed'"
                "                  AND s.brand_id = $2::uuid)"
                "    OR NOT EXISTS (SELECT 1 FROM orders o"
                "                   WHERE o.customer_id = $1"
                "                     AND o.status = 'completed')",
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


def check_ip_rate_limit(ip: str, *, route: str, limit: int = 120,
                        window_s: int = 60) -> None:
    """Fixed-window per-IP cap for a route with no session to key on.

    Used by the pre-login staff directory: an unauthenticated caller cannot
    be keyed by staff_code, so the only stable identity is the address.
    """
    now = time.monotonic()
    key = (f"ip:{ip}", route)
    start, count = _rate_counters[key]
    if now - start > window_s:
        _rate_counters[key] = [now, 1]
        return
    if count >= limit:
        raise HTTPException(status_code=429, detail={"code": "rate_limited"})
    _rate_counters[key][1] += 1


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


# --- login hardening (review 7.2-5) ---------------------------------------

#: Failures allowed per (client ip, identifier) inside one window. Only
#: FAILURES are counted: a correct password is never locked out by a guesser
#: sharing the identifier, and a burst of legitimate sign-ins is unaffected.
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW_S = 60

#: A valid bcrypt hash whose preimage is thrown away. Every login failure path
#: runs one comparison against it, so an unknown identifier costs the same
#: wall-clock time as a wrong password — the latency itself used to enumerate
#: accounts, because bcrypt ran only for accounts that exist.
LOGIN_DUMMY_HASH = "$2b$12$HjoJQUurBEui.OQKTk4OxOiFLxeCkFJNGiV0FLzXXuF2oBJlJ7aTq"


def client_ip(request: Request) -> str:
    """Caller address for rate-limit keys (first hop when proxied)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _login_key(identifier: str, ip: str) -> tuple[str, str]:
    return (f"login:{ip}:{identifier.strip().lower()}", "login")


def check_login_rate_limit(identifier: str, ip: str) -> None:
    """Generic 429 once this (ip, identifier) has burned its failure budget.

    Says nothing about whether the account exists, and is reached only by
    repeated failures, so a legitimate sign-in never meets it.
    """
    now = time.monotonic()
    key = _login_key(identifier, ip)
    start, count = _rate_counters[key]
    if now - start > LOGIN_FAILURE_WINDOW_S:
        return
    if count >= LOGIN_FAILURE_LIMIT:
        raise HTTPException(status_code=429, detail={"code": "rate_limited"})


def note_login_failure(identifier: str, ip: str) -> None:
    """Count one failed attempt; called from every login failure path."""
    now = time.monotonic()
    key = _login_key(identifier, ip)
    start, count = _rate_counters[key]
    if now - start > LOGIN_FAILURE_WINDOW_S:
        _rate_counters[key] = [now, 1]
        return
    _rate_counters[key] = [start, count + 1]


class NoBodyLoggingFilter(logging.Filter):
    """Drop log records carrying request bodies for PII routes.

    Attach to uvicorn access loggers; any record whose message mentions
    a PII route with a body payload is suppressed. Route handlers must
    also never log search bodies explicitly.

    The list is a PII list, not a customers list: POST /orders/search takes
    a customer NAME in its body, exactly like POST /customers/search, so a
    body log of it is the same leak under a different route.
    """

    PII_PATHS = ("/customers/search", "/orders/search")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(p in msg and "body" in msg.lower() for p in self.PII_PATHS)


# `Cache-Control: no-store` is FAIL-CLOSED: every route under /api/v1 carries it
# unless its first path segment is listed in CACHEABLE_NAMESPACES as
# non-personal reference data. The earlier shape was an allow-list of PII
# prefixes, so a new PII router stayed cacheable until someone remembered this
# file - review 7.2-9 missed /analytics (member-level rows) and /uploads (staged
# raw customer files) exactly that way, and the test that guarded it asserted
# only the prefixes already listed.
#
# test_every_api_namespace_is_classified enumerates the app's own route table, so
# a namespace nobody has classified fails the suite instead of shipping. Adding
# a PII namespace here is a no-op at runtime (no-store is the default); moving
# one to CACHEABLE_NAMESPACES is the only way to turn the header off.
NO_STORE_NAMESPACES = (
    # customer rows, search, consents, events, custom data, orders,
    # member-level analytics, staged raw files, accounts + staff roster,
    # and the internal workspaces that embed customer rows
    "analytics",
    "auth",
    "campaigns",
    "connectors",
    "customers",
    "groups",
    "orders",
    "pipelines",
    "reports",
    "staff",
    "stats",
    "sync-runs",
    "uploads",
    "users",
)

# Reference/catalogue reads: no customer rows, and caching them is the point.
# Everything not named here is no-store.
CACHEABLE_NAMESPACES = (
    "brands",
    "inventory",
    "products",
    "reference",
    "stores",
    "tiers",
)


def _wants_no_store(path: str) -> bool:
    """True for every /api/v1 route whose namespace is not declared cacheable."""
    if not path.startswith("/api/v1/"):
        return False
    namespace = path.split("/")[3]
    return namespace not in CACHEABLE_NAMESPACES


async def no_store_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Middleware: fail-closed `Cache-Control: no-store` under /api/v1."""
    response = await call_next(request)
    if _wants_no_store(request.url.path):
        response.headers["Cache-Control"] = "no-store"
    return response


# --- one error shape (A120) -------------------------------------------------
#
# Every failure this API answers has ONE body:
#
#     {"detail": {"code": ..., "message": ..., "field": ..., "request_id": ...}}
#
# `code` is the machine name a client branches on, `message` a human
# sentence, `field` the request field the failure is about (null when the
# failure is about the request as a whole), and `request_id` the id of THIS
# request -- the same id the `x-request-id` response header carries, so one
# string leads from a reader's screenshot back to the server log line.
#
# The legacy shapes survive inside `detail`: a route that raised
# HTTPException(detail={"code": ..., "available_k": [...]}) keeps every key
# it had, and a route that raised a plain string keeps that string as the
# message. A client that reads `detail.code` today keeps working; a client
# that read `detail` as a string now reads `detail.message` instead.

#: The header the request id travels in (request AND response).
REQUEST_ID_HEADER = "x-request-id"

#: Default machine code per status, for a failure that arrives without one
#: (a plain HTTPException string, an unrouted path).
STATUS_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "unprocessable",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}

#: The sentence for a status whose code has no sentence of its own.
STATUS_ERROR_MESSAGES = {
    400: "The request was not accepted as sent.",
    401: "Sign in to continue.",
    403: "You do not have access to this resource.",
    404: "That resource does not exist.",
    405: "That method is not allowed on this resource.",
    409: "That conflicts with what is already on file.",
    413: "That request is larger than this service accepts.",
    422: "The request was not accepted as sent.",
    429: "Too many requests. Wait a moment and try again.",
    500: "The server could not complete this request.",
    503: "The server is not ready to answer that yet.",
}

#: The human sentence for a machine code. A code with no sentence here gets
#: the sentence for its status, so the message is never empty and never
#: says something the code does not mean.
ERROR_MESSAGES = {
    "missing_session": "Sign in to continue.",
    "invalid_session": "This session is not valid any more. Sign in again.",
    "expired_session": "This session has expired. Sign in again.",
    "login_disabled": "This account is switched off.",
    "invalid_credentials": "That identifier and password do not match.",
    "invalid_current_password": "The current password is not correct.",
    "weak_password": "That password is not strong enough.",
    "forbidden": "You do not have access to this resource.",
    "not_found": "That resource does not exist.",
    "internal_error": "The server could not complete this request.",
    "validation_error": "The request was not accepted as sent.",
    "rate_limited": "Too many requests. Wait a moment and try again.",
    "pii_in_url": "Personal data must not be sent in a URL.",
    "no_snapshot": "There is no snapshot for this question yet.",
    "no_analytics_scope": (
        "This account has no analytics scope: it is not linked to a "
        "brand, so there is nothing to read."),
    "snapshot_not_found": "There is no snapshot for this question yet.",
    "unfitted_snapshot": "That snapshot was not fitted, so it has nothing to show.",
    "no_transactions": "There are no transactions to work from yet.",
    "no_content": "There is nothing there.",
    "no_history": "There is no history for this yet.",
    "empty_file": "The file was empty.",
    "file_too_large": "That file is larger than this upload accepts.",
    "invalid_contact": "That contact value is not valid.",
    "duplicate_contact": "That contact value is already on file.",
    "duplicate_external_member_id": "That member id is already on file.",
    "customer_not_found": "That customer does not exist.",
    "member_not_found": "That member does not exist.",
    "product_not_found": "That product does not exist.",
    "store_not_found": "That store does not exist.",
    "staff_not_found": "That staff record does not exist.",
    "connector_not_found": "That connector does not exist.",
    "report_not_found": "That report does not exist.",
}


def request_id_of(request: Request) -> str:
    """The id minted for THIS request ("" when there is none)."""
    try:
        return getattr(request.state, "request_id", "") or ""
    except Exception:  # pragma: no cover - a request without a state
        return ""


def error_detail(
    code: str,
    *,
    status_code: int = 500,
    message: str | None = None,
    field: str | None = None,
    request_id: str | None = None,
    extra: dict | None = None,
) -> dict:
    """The ONE error object every failure carries.

    `extra` holds the route's own keys (the legacy `detail` object's data
    keys): they are kept, never dropped, so a client reading them today
    still finds them.
    """
    detail: dict = {"code": code}
    detail["message"] = (
        message
        or ERROR_MESSAGES.get(code)
        or STATUS_ERROR_MESSAGES.get(status_code, "The request could not be completed.")
    )
    detail["field"] = field
    if request_id:
        detail["request_id"] = request_id
    if extra:
        for key, value in extra.items():
            detail.setdefault(key, value)
    return detail


def http_error_detail(detail, status_code: int, *, request_id: str | None = None) -> dict:
    """Normalise whatever a route raised into the one error object."""
    if isinstance(detail, dict):
        code = detail.get("code") or STATUS_ERROR_CODES.get(status_code, "error")
        message = detail.get("message") or detail.get("detail")
        field = detail.get("field")
        extra = {
            k: v for k, v in detail.items()
            if k not in ("code", "message", "field")
        }
        return error_detail(
            code,
            status_code=status_code,
            message=message if isinstance(message, str) and message else None,
            field=field if isinstance(field, str) else None,
            request_id=request_id,
            extra=extra,
        )
    if isinstance(detail, list):
        # FastAPI's validation list, if a route raised one directly.
        return validation_error_detail(detail, request_id=request_id)
    if isinstance(detail, str) and detail:
        return error_detail(
            STATUS_ERROR_CODES.get(status_code, "error"),
            status_code=status_code,
            message=detail,
            request_id=request_id,
        )
    return error_detail(
        STATUS_ERROR_CODES.get(status_code, "error"),
        status_code=status_code,
        request_id=request_id,
    )


def validation_error_detail(errors, *, request_id: str | None = None) -> dict:
    """A request-validation failure in the one shape: code + field + message.

    `field` names the field the request got wrong (`body.contacts.0.value`),
    so the page can point at the input instead of saying "invalid".
    """
    items = []
    for err in errors or ():
        if not isinstance(err, dict):
            continue
        loc = err.get("loc") or ()
        items.append({
            "loc": [str(p) for p in loc],
            "msg": str(err.get("msg") or ""),
            "type": str(err.get("type") or ""),
        })
    first = items[0] if items else {}
    field = ".".join(first.get("loc") or ()) or None
    message = first.get("msg") or None
    return error_detail(
        "validation_error",
        status_code=422,
        message=message,
        field=field,
        request_id=request_id,
        extra={"errors": items},
    )


class RequestIdMiddleware:
    """Mint ONE request id per request and put it on the response.

    The id lives in `scope["state"]` (Starlette's `Request.state`), so the
    error handlers, the log lines and the response header all name the same
    request -- there is no second id anywhere.
    """

    def __init__(self, app):  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_wrapper(message):  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_wrapper)
