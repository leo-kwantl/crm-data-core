"""Store endpoints: CRUD with contact columns (auto-mounted).

- GET /stores — cursor list, newest-first.
- GET /stores/:id — detail with order_count.
- POST /stores — 201; 409 duplicate code.
- PATCH /stores/:id — name/address/phone only (code immutable); 404 missing.
- DELETE /stores/:id — 204; 409 store_has_orders while orders reference it.

Non-PII resource: no requirement change vs other routers — the
session (x-session-token header) is still required for a uniform audit posture.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from ..deps import CurrentUser, check_brand, get_current_user, get_pool, is_group_wide
from ..repos import groups as groups_repo
from ..repos import stores as repo
from ..repos.paging import clamp_page_size
from ..schemas.store import StoreCreate, StoreUpdate

router = APIRouter(tags=["stores"])


@router.get("/stores")
async def list_stores(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int, Query()] = 20,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> dict:
    brand_id, group_id = _scope(user)
    if page is not None:
        return await repo.list_stores_offset(
            pool, page=page, page_size=clamp_page_size(page_size),
            brand_id=brand_id, group_id=group_id, q=q,
        )
    try:
        items, next_cursor = await repo.list_stores_page(
            pool, limit=limit, cursor=cursor,
            brand_id=brand_id, group_id=group_id, q=q,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "invalid_cursor"})
    return {"data": items, "next_cursor": next_cursor}


@router.get("/stores/{store_id}")
async def read_store(
    store_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    data = await repo.get_store(pool, store_id)
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "store_not_found"})
    _guard_row(user, data.get("brand_id"), data.get("group_id"))
    return data


@router.post("/stores", status_code=status.HTTP_201_CREATED)
async def create_store(
    payload: StoreCreate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    brand_id = await _resolve_brand(pool, user, payload.brand_id)
    try:
        return await repo.create_store(
            pool,
            code=payload.code,
            name=payload.name,
            address=payload.address,
            phone=payload.phone,
            brand_id=brand_id,
        )
    except repo.DuplicateCodeError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "duplicate_store_code", "value": str(exc)}
        )


@router.patch("/stores/{store_id}")
async def patch_store(
    store_id: UUID,
    payload: StoreUpdate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    current = await repo.get_store(pool, store_id)
    if current is None:
        raise HTTPException(status_code=404, detail={"code": "store_not_found"})
    _guard_row(user, current.get("brand_id"), current.get("group_id"))
    data = await repo.update_store(
        pool,
        store_id,
        name=payload.name,
        address=payload.address,
        clear_address=payload.clear_address,
        phone=payload.phone,
        clear_phone=payload.clear_phone,
    )
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "store_not_found"})
    return data


@router.delete("/stores/{store_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_store(
    store_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> Response:
    current = await repo.get_store(pool, store_id)
    if current is None:
        raise HTTPException(status_code=404, detail={"code": "store_not_found"})
    _guard_row(user, current.get("brand_id"), current.get("group_id"))
    try:
        gone = await repo.delete_store(pool, store_id)
    except repo.LinkedError:
        raise HTTPException(status_code=409, detail={"code": "store_has_orders"})
    if gone is None:
        raise HTTPException(status_code=404, detail={"code": "store_not_found"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _scope(user: CurrentUser) -> tuple[UUID | None, UUID | None]:
    """(brand filter, group filter) for list queries.

    Brand-scoped sa: exact brand match. Group-wide roles (manager/admin): no
    brand filter, but still siloed to their own group when the session
    carries one (brand-less HQ sessions pass None = every group)."""
    if is_group_wide(user):
        return None, user.group_id
    return user.brand_id, None


def _guard_row(
    user: CurrentUser, brand_id: str | None, group_id: str | None
) -> None:
    """Detail/mutation guard: cross-brand reads/mutations are 403 forbidden
    for brand-scoped staff; rows outside a group-wide session's group are
    404 (never leak cross-group existence). Legacy NULL-brand rows stay
    readable group-wide via check_brand (see deps)."""
    check_brand(user, brand_id)
    if (
        is_group_wide(user)
        and user.group_id is not None
        and group_id is not None
        and str(group_id) != str(user.group_id)
    ):
        raise HTTPException(status_code=404, detail={"code": "store_not_found"})


async def _resolve_brand(
    pool: asyncpg.Pool, user: CurrentUser, explicit: UUID | None
) -> UUID:
    """Brand for a new store. Explicit wins (must exist + stay in-group,
    else 404/403); else the session brand; else the DEMO holder."""
    if explicit is not None:
        brand = await groups_repo.get_brand(pool, explicit)
        if brand is None:
            raise HTTPException(status_code=404, detail={"code": "brand_not_found"})
        check_brand(user, explicit)
        if (
            user.group_id is not None
            and brand["group_id"] != str(user.group_id)
        ):
            raise HTTPException(status_code=403, detail={"code": "forbidden"})
        return explicit
    check_brand(user, user.brand_id)
    return await groups_repo.resolve_write_brand(
        pool, explicit=None, session_brand_id=user.brand_id
    )
