"""Customer journey-event endpoints (auto-mounted by app.py discovery).

- POST /customers/{id}/events — append one event (201).
- GET /customers/{id}/events — cursor-paginated timeline, newest-first,
  with customer member_id/name header fields for timeline context.
- No PUT/PATCH/DELETE: timeline is append-only (405).

PII route: requires a session (x-session-token header); no-store set by middleware.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..deps import (
    CurrentUser,
    get_current_user,
    get_pool,
    producer_of,
    require_customer_in_scope,
)
from ..repos import customer_events as repo
from ..repos import reference as refrepo
from ..repos.paging import clamp_page_size
from ..schemas.customer_event import CustomerEventCreate

router = APIRouter(tags=["customer-events"])


def _not_found(customer_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"code": "customer_not_found", "customer_id": str(customer_id)}
    )


@router.post(
    "/customers/{customer_id}/events",
    response_model=dict,
    status_code=201,
)
async def append_event(
    customer_id: uuid.UUID,
    body: CustomerEventCreate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    await require_customer_in_scope(pool, customer_id, user)
    async with pool.acquire() as conn:
        if not await repo.customer_exists(conn, customer_id):
            raise _not_found(customer_id)
        for domain, code in (("event_kind", body.kind), ("event_source", body.source)):
            try:
                await refrepo.require_active(conn, domain, code)
            except refrepo.UnknownCodeError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": f"unknown_{domain}",
                        "domain": exc.domain,
                        "value": exc.value,
                    },
                )
        row = await repo.insert_event(
            conn,
            customer_id,
            kind=body.kind,
            source=body.source,
            summary=body.summary,
            payload=body.payload,
            # The producer is the acting session, never the caller: a
            # client-supplied name would let one session write a row that
            # names another, and a session without a staff row would write
            # a row that names nobody.
            created_by=producer_of(user),
        )
    return dict(row)


@router.get("/customers/{customer_id}/events")
async def list_events(
    customer_id: uuid.UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int, Query()] = 20,
) -> dict:
    _ = user  # session identity: required for the audit trail
    await require_customer_in_scope(pool, customer_id, user)
    size = clamp_page_size(page_size)
    async with pool.acquire() as conn:
        header = await repo.get_customer_header(conn, customer_id)
        if header is None:
            raise _not_found(customer_id)
        if page is not None:
            return await repo.list_customer_events_offset(
                conn, customer_id, page=page, page_size=size
            )
        try:
            rows, next_cursor = await repo.list_customer_events(
                conn, customer_id, limit=limit, cursor=cursor
            )
        except ValueError:
            raise HTTPException(status_code=400, detail={"code": "invalid_cursor"})
    return {
        "customer_id": customer_id,
        "customer_member_id": header["member_id"],
        "customer_name": header["full_name"],
        "data": rows,
        "next_cursor": next_cursor,
    }


async def _method_not_allowed() -> Response:
    return Response(status_code=405)


for _method in ("PUT", "PATCH", "DELETE"):
    router.add_api_route(
        "/customers/{customer_id}/events",
        _method_not_allowed,
        methods=[_method],
        include_in_schema=False,
    )
