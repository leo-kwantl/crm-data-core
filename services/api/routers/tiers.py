"""Tier + loyalty endpoints (auto-mounted).

- GET /tiers — full list ordered by rank (small table, no cursor).
- POST /tiers — 201; 409 duplicate_tier_code.
- PATCH /tiers/:code — name/rank/benefits only (code immutable); 404 missing.
- PUT /customers/:id/tier {tier_code|null} — manual assign/unassign;
  404 customer_not_found, 422 tier_not_found for unknown codes.
- GET /customers/:id/loyalty — {tier, order_count, total_spent_cents};
  NEW endpoint so the customer profile schema/repo stay untouched.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import (
    CurrentUser,
    check_brand,
    get_current_user,
    get_pool,
    is_group_wide,
)
from ..repos import customers as customers_repo
from ..repos import groups as groups_repo
from ..repos import tiers as repo
from ..schemas.tier import TierAssign, TierCreate, TierUpdate

router = APIRouter(tags=["tiers"])


@router.get("/tiers")
async def list_tiers(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    brand_id = None if is_group_wide(user) else user.brand_id
    return {"data": await repo.list_tiers(pool, brand_id=brand_id)}


@router.post("/tiers", status_code=status.HTTP_201_CREATED)
async def create_tier(
    payload: TierCreate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    brand_id = await _resolve_tier_brand(pool, user, payload.brand_id)
    try:
        return await repo.create_tier(
            pool,
            code=payload.code,
            name=payload.name,
            rank=payload.rank,
            benefits_text=payload.benefits_text,
            brand_id=brand_id,
        )
    except repo.DuplicateCodeError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "duplicate_tier_code", "value": str(exc)}
        )


@router.patch("/tiers/{code}")
async def patch_tier(
    code: str,
    payload: TierUpdate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = user
    data = await repo.update_tier(
        pool,
        code,
        name=payload.name,
        rank=payload.rank,
        benefits_text=payload.benefits_text,
        clear_benefits=payload.clear_benefits,
    )
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "tier_not_found"})
    return data


@router.put("/customers/{customer_id}/tier")
async def set_customer_tier(
    customer_id: UUID,
    payload: TierAssign,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    await _guard_customer_group(pool, user, customer_id)
    result = await repo.assign_tier(pool, customer_id, payload.tier_code)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "customer_not_found"})
    if isinstance(result, tuple):
        raise HTTPException(status_code=422, detail={"code": result[1]})
    return result


@router.get("/customers/{customer_id}/loyalty")
async def read_loyalty(
    customer_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    await _guard_customer_group(pool, user, customer_id)
    data = await repo.get_loyalty(pool, customer_id)
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "customer_not_found"})
    return data


async def _guard_customer_group(
    pool: asyncpg.Pool, user: CurrentUser, customer_id: UUID
) -> None:
    """404 when the customer lives outside the session group (never leak
    cross-group existence). Brand-less group-wide sessions skip the check."""
    if user.group_id is None:
        return
    group = await customers_repo.group_of_customer(pool, customer_id)
    if group is None:
        raise HTTPException(status_code=404, detail={"code": "customer_not_found"})
    if group != str(user.group_id):
        raise HTTPException(status_code=404, detail={"code": "customer_not_found"})


async def _resolve_tier_brand(
    pool: asyncpg.Pool, user: CurrentUser, explicit: UUID | None
) -> UUID | None:
    """Brand for a new tier. Explicit wins (must exist + stay in-group);
    else the session brand for sa; group-wide roles keep None (= global
    catalogue row) unless they name a brand explicitly."""
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
    if is_group_wide(user):
        return None
    return user.brand_id
