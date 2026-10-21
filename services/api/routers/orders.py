"""Read-only purchase history endpoints (auto-mounted by app.py discovery).

- GET /customers/{id}/orders — cursor-paginated, newest-first.
- GET /orders/{id} — header + line items.
- POST /orders/search (routers/orders_search.py) — keyword search, body-only.
- Any write (POST/PUT/PATCH/DELETE) on /orders* -> 405 (orders immutable via API).
  The {order_id} guard is UUID-scoped so it cannot shadow POST /orders/search.

PII route: requires a session (x-session-token header); no-store set by middleware.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ..deps import (
    CurrentUser,
    get_current_user,
    get_pool,
    require_customer_in_scope,
    session_brand,
)
from ..repos import orders as repo
from ..repos.paging import clamp_page_size

router = APIRouter(tags=["orders"])

_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


@router.get("/orders")
async def list_orders_global(
    request: Request,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int, Query()] = 20,
    store_code: Annotated[str | None, Query()] = None,
    date_from: Annotated[_dt.date | None, Query()] = None,
    date_to: Annotated[_dt.date | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> dict:
    """Global order list, newest-first with store + customer display fields.

    Optional filters (additive): ?store_code=, ?date_from=/?date_to=
    (YYYY-MM-DD, inclusive), ?status= (exact). Cursor + offset modes honour
    all.

    SCOPE: the list is global only for a group-wide or HQ session. A
    brand-scoped session reads ITS brand's orders (the session's own brand,
    deps.session_brand) -- the same rule the customers list follows, and the
    same rule `db/queries/orders.sql` carries for the mirror. A brand
    session filtering by another brand's store code gets an empty page, not
    that brand's orders.

    A keyword search (?q= order UUID prefix / member_id / customer NAME) is
    NOT accepted here: it would put a customer name in the URL. Use
    POST /orders/search (body-only) instead.

    Line items stay on GET /orders/{order_id} (detail endpoint)."""
    # Reject PII in URL: a keyword must never reach the database through
    # the query string (browser history, access log, Referer).
    if "q" in request.query_params:
        raise HTTPException(status_code=422, detail={"code": "pii_in_url"})
    _ = user  # session identity: required for the audit trail
    size = clamp_page_size(page_size)
    async with pool.acquire() as conn:
        if page is not None:
            return await repo.list_orders_global_offset(
                conn, page=page, page_size=size,
                store_code=store_code, date_from=date_from, date_to=date_to,
                status=status, q=None, brand_id=session_brand(user),
            )
        try:
            rows, next_cursor = await repo.list_orders_global(
                conn, limit=limit, cursor=cursor,
                store_code=store_code, date_from=date_from, date_to=date_to,
                status=status, q=None, brand_id=session_brand(user),
            )
        except ValueError:
            raise HTTPException(status_code=400, detail={"code": "invalid_cursor"})
    return {"data": rows, "next_cursor": next_cursor}


@router.get("/customers/{customer_id}/orders")
async def list_orders(
    customer_id: uuid.UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int, Query()] = 20,
    store_code: Annotated[str | None, Query()] = None,
    date_from: Annotated[_dt.date | None, Query()] = None,
    date_to: Annotated[_dt.date | None, Query()] = None,
) -> dict:
    _ = user  # session identity: required for the audit trail
    await require_customer_in_scope(pool, customer_id, user)
    size = clamp_page_size(page_size)
    try:
        async with pool.acquire() as conn:
            if not await repo.customer_exists(conn, customer_id):
                raise HTTPException(status_code=404, detail={"code": "not_found"})
            if page is not None:
                return await repo.list_customer_orders_offset(
                    conn, customer_id, page=page, page_size=size,
                    store_code=store_code, date_from=date_from, date_to=date_to,
                )
            try:
                rows, next_cursor = await repo.list_customer_orders(
                    conn, customer_id, limit=limit, cursor=cursor,
                    store_code=store_code, date_from=date_from, date_to=date_to,
                )
            except ValueError:
                raise HTTPException(status_code=400, detail={"code": "invalid_cursor"})
    except HTTPException:
        raise
    return {"data": rows, "next_cursor": next_cursor}


@router.get("/orders/{order_id}")
async def get_order(
    order_id: uuid.UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = user
    async with pool.acquire() as conn:
        detail = await repo.get_order_with_items(conn, order_id)
    if detail is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return detail


async def _method_not_allowed() -> Response:
    return Response(status_code=405)


for _path in ("/orders", "/orders/{order_id:uuid}", "/customers/{customer_id}/orders"):
    for _method in _WRITE_METHODS:
        router.add_api_route(
            _path,
            _method_not_allowed,
            methods=[_method],
            include_in_schema=False,
        )
