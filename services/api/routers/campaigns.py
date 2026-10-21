"""Campaign endpoints: promotion-first CRUD + member assignment (auto-mounted).

Status machine enforced: draft -> active -> ended (same-status PATCH is a
no-op). Member writes on ended campaigns -> 422 campaign_ended; duplicate
assignment -> 409 duplicate_member.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from ..deps import (
    CurrentUser,
    check_brand,
    get_current_user,
    get_pool,
    is_group_wide,
    require_customer_in_scope,
)
from ..repos import campaigns as repo
from ..repos import groups as groups_repo
from ..repos.reference import UnknownCodeError
from ..repos.paging import clamp_page_size
from ..schemas.campaign import CampaignCreate, CampaignMemberAdd, CampaignUpdate

router = APIRouter(tags=["campaigns"])


@router.get("/campaigns")
async def list_campaigns(
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
        return await repo.list_campaigns_offset(
            pool, page=page, page_size=clamp_page_size(page_size),
            brand_id=brand_id, group_id=group_id, q=q,
        )
    try:
        items, next_cursor = await repo.list_campaigns_page(
            pool, limit=limit, cursor=cursor,
            brand_id=brand_id, group_id=group_id, q=q,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "invalid_cursor"})
    return {"data": items, "next_cursor": next_cursor}


@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CampaignCreate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    brand_id = await _resolve_brand(pool, user, payload.brand_id)
    try:
        return await repo.create_campaign(
            pool,
            code=payload.code,
            name=payload.name,
            kind=payload.kind,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            config=payload.config,
            brand_id=brand_id,
        )
    except UnknownCodeError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "unknown_campaign_kind", "domain": exc.domain, "value": exc.value},
        )
    except repo.DuplicateCodeError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "duplicate_campaign_code", "value": str(exc)}
        )


@router.get("/campaigns/{campaign_id}")
async def read_campaign(
    campaign_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    data = await repo.get_campaign(pool, campaign_id)
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
    _guard_row(user, data.get("brand_id"), data.get("group_id"), "campaign_not_found")
    return data


@router.patch("/campaigns/{campaign_id}")
async def patch_campaign(
    campaign_id: UUID,
    payload: CampaignUpdate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    current = await repo.get_campaign(pool, campaign_id)
    if current is None:
        raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
    _guard_row(user, current.get("brand_id"), current.get("group_id"), "campaign_not_found")
    try:
        data = await repo.update_campaign(
            pool,
            campaign_id,
            name=payload.name,
            status=payload.status,
            starts_at=payload.starts_at,
            clear_starts_at=payload.clear_starts_at,
            ends_at=payload.ends_at,
            clear_ends_at=payload.clear_ends_at,
            config=payload.config,
        )
    except UnknownCodeError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "unknown_campaign_status", "domain": exc.domain, "value": exc.value},
        )
    except repo.IllegalTransitionError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "illegal_transition", "value": str(exc)}
        )
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
    return data


@router.delete("/campaigns/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(
    campaign_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> Response:
    current = await repo.get_campaign(pool, campaign_id)
    if current is None:
        raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
    _guard_row(user, current.get("brand_id"), current.get("group_id"), "campaign_not_found")
    if not await repo.delete_campaign(pool, campaign_id):
        raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/campaigns/{campaign_id}/members")
async def list_members(
    campaign_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
) -> dict:
    campaign = await repo.get_campaign(pool, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
    _guard_row(user, campaign.get("brand_id"), campaign.get("group_id"), "campaign_not_found")
    try:
        items, next_cursor = await repo.list_members_page(
            pool, campaign_id, limit=limit, cursor=cursor
        )
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "invalid_cursor"})
    if items is None:
        raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
    return {"data": items, "next_cursor": next_cursor}


@router.post("/campaigns/{campaign_id}/members", status_code=status.HTTP_201_CREATED)
async def add_member(
    campaign_id: UUID,
    payload: CampaignMemberAdd,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    campaign = await repo.get_campaign(pool, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
    _guard_row(user, campaign.get("brand_id"), campaign.get("group_id"), "campaign_not_found")
    await require_customer_in_scope(pool, payload.customer_id, user)
    result = await repo.add_member(
        pool, campaign_id, payload.customer_id, assigned_by=user.staff_code
    )
    if isinstance(result, tuple):
        _, code = result
        if code == "campaign_not_found":
            raise HTTPException(status_code=404, detail={"code": code})
        if code == "duplicate_member":
            raise HTTPException(status_code=409, detail={"code": code})
        raise HTTPException(status_code=422, detail={"code": code})
    return result


@router.delete(
    "/campaigns/{campaign_id}/members/{customer_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    campaign_id: UUID,
    customer_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> Response:
    campaign = await repo.get_campaign(pool, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
    _guard_row(user, campaign.get("brand_id"), campaign.get("group_id"), "campaign_not_found")
    if not await repo.remove_member(pool, campaign_id, customer_id):
        raise HTTPException(status_code=404, detail={"code": "member_not_found"})
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
    user: CurrentUser,
    brand_id: str | None,
    group_id: str | None,
    not_found_code: str,
) -> None:
    """Cross-brand reads/mutations are 403 forbidden for brand-scoped staff;
    rows outside a group-wide session's group are 404 (never leak
    cross-group existence). Legacy NULL-brand rows stay readable group-wide
    via check_brand (see deps)."""
    check_brand(user, brand_id)
    if (
        is_group_wide(user)
        and user.group_id is not None
        and group_id is not None
        and str(group_id) != str(user.group_id)
    ):
        raise HTTPException(status_code=404, detail={"code": not_found_code})



async def _resolve_brand(
    pool: asyncpg.Pool, user: CurrentUser, explicit: UUID | None
) -> UUID:
    """Brand for a new campaign. Explicit wins (must exist + stay in-group,
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
