"""Group/brand admin endpoints (auto-mounted).

- GET /groups — list. Brand/manager sessions see only their own group
  (group silo); brand-less HQ sessions (admin, no staff link) see every group.
- POST /groups — admin-only create; 409 duplicate_group_code.
- GET /brands[?group_id=] — list, siloed to the session group by default;
  an explicit group_id outside the session group is 403 forbidden.
- POST /brands — admin-only create under an existing group;
  404 group_not_found, 409 duplicate_brand_code.
- GET /brands/:id — detail; cross-group reads are 403 forbidden
  (brand-scoped staff) or 404 (group-wide roles, never leak existence).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..deps import (
    CurrentUser,
    get_current_user,
    get_pool,
    is_group_wide,
    require_admin,
)
from ..repos import groups as repo
from ..schemas.group import BrandCreate, BrandOut, GroupCreate, GroupOut

router = APIRouter(tags=["groups"])


@router.get("/groups", response_model=list[GroupOut])
async def list_groups(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> list[dict]:
    return await repo.list_groups_scoped(pool, group_id=user.group_id)


@router.post("/groups", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreate,
    user: Annotated[CurrentUser, Depends(require_admin)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = user
    try:
        row = await repo.create_group(pool, code=payload.code, name=payload.name)
    except repo.DuplicateCodeError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "duplicate_group_code", "value": str(exc)}
        )
    row["brand_count"] = 0
    return row


@router.get("/brands", response_model=list[BrandOut])
async def list_brands(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    group_id: Annotated[UUID | None, Query()] = None,
) -> list[dict]:
    if group_id is not None:
        _guard_group_param(user, group_id)
        return await repo.list_brands(pool, group_id=group_id)
    # Default silo: own group for sessions that carry one, every brand for HQ.
    return await repo.list_brands(pool, group_id=user.group_id)


@router.post("/brands", response_model=BrandOut, status_code=status.HTTP_201_CREATED)
async def create_brand(
    payload: BrandCreate,
    user: Annotated[CurrentUser, Depends(require_admin)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _guard_group_param(user, payload.group_id)
    try:
        return await repo.create_brand(
            pool,
            group_id=payload.group_id,
            code=payload.code,
            name=payload.name,
        )
    except repo.NotFoundError:
        raise HTTPException(status_code=404, detail={"code": "group_not_found"})
    except repo.DuplicateCodeError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "duplicate_brand_code", "value": str(exc)}
        )


@router.get("/brands/{brand_id}", response_model=BrandOut)
async def read_brand(
    brand_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    data = await repo.get_brand(pool, brand_id)
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "brand_not_found"})
    if user.group_id is not None and data["group_id"] != str(user.group_id):
        if is_group_wide(user):
            # Never leak cross-group existence to group-wide roles.
            raise HTTPException(status_code=404, detail={"code": "brand_not_found"})
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    return data


def _guard_group_param(user: CurrentUser, group_id: UUID) -> None:
    """Explicit group params outside the session group are 403 forbidden."""
    if user.group_id is not None and str(group_id) != str(user.group_id):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
