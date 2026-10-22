"""Customer profile routes: POST/GET/PATCH/DELETE.

Cache-Control: no-store is applied by middleware (see deps.no_store_headers).
Never log request bodies here (PII rule).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from ..deps import (
    CurrentUser,
    check_search_rate_limit,
    get_current_user,
    get_pool,
    require_customer_in_scope,
)
from ..repos import customers as repo
from ..repos import search as search_repo
from ..schemas.customer import CustomerCreate, CustomerProfile, CustomerUpdate
from . import search as search_router

router = APIRouter(tags=["customers"])


def _to_profile(data: dict) -> CustomerProfile:
    return CustomerProfile(**data)


@router.post("/customers", response_model=CustomerProfile, status_code=status.HTTP_201_CREATED)
async def create_customer(payload: CustomerCreate,
                          user: CurrentUser = Depends(get_current_user),
                          pool=Depends(get_pool)):
    try:
        data = await repo.create_customer(
            pool,
            full_name=payload.full_name,
            contacts=[c.model_dump() for c in payload.contacts],
            attributes=payload.attributes,
            external_member_id=payload.external_member_id,
            gender=payload.gender,
            date_of_birth=payload.date_of_birth,
            district=payload.district,
            address_line=payload.address_line,
        )
    except repo.DuplicateExternalIdError as exc:
        raise HTTPException(status_code=409, detail={"code": "duplicate_external_member_id", "value": str(exc)})
    except repo.DuplicateContactError as exc:
        raise HTTPException(status_code=409, detail={"code": "duplicate_contact", "value": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_contact", "detail": str(exc)})
    return _to_profile(data)


@router.get("/customers/{customer_id}", response_model=CustomerProfile)
async def read_customer(customer_id: UUID,
                        user: CurrentUser = Depends(get_current_user),
                        pool=Depends(get_pool)):
    # A UUID fetch is a PII lookup: the search router's contract says every
    # one of its calls appends one `customer_access_logs` row (criteria type
    # only) and is rate-limited per session. This route is the one that
    # answers -- the search router's route for the same path is registered
    # later (the routers are included in alphabetical module order) and never
    # runs -- so the audit and the limit live here.
    check_search_rate_limit(
        user.staff_code,
        limit=search_router.RATE_LIMIT,
        window_s=search_router.RATE_WINDOW_S,
        route="search:uuid",
    )
    data = await repo.get_customer(pool, customer_id)
    async with pool.acquire() as conn:
        await search_repo.write_audit(
            conn,
            staff_id=user.staff_code,
            customer_id=customer_id if data else None,
            criteria_type="uuid",
            result_count=1 if data else 0,
        )
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "customer_not_found"})
    await require_customer_in_scope(pool, customer_id, user)
    return _to_profile(data)


@router.patch("/customers/{customer_id}", response_model=CustomerProfile)
async def patch_customer(customer_id: UUID, payload: CustomerUpdate,
                         user: CurrentUser = Depends(get_current_user),
                         pool=Depends(get_pool)):
    existing = await repo.get_customer(pool, customer_id)
    if existing is None:
        raise HTTPException(status_code=404, detail={"code": "customer_not_found"})
    await require_customer_in_scope(pool, customer_id, user)
    try:
        fields_set = payload.model_fields_set
        data = await repo.update_customer(
            pool,
            customer_id,
            full_name=payload.full_name,
            attributes=payload.attributes,
            contacts=[c.model_dump() for c in payload.contacts] if payload.contacts is not None else None,
            external_member_id=payload.external_member_id,
            external_member_id_set="external_member_id" in fields_set,
            gender=payload.gender,
            gender_set="gender" in fields_set,
            date_of_birth=payload.date_of_birth,
            date_of_birth_set="date_of_birth" in fields_set,
            district=payload.district,
            district_set="district" in fields_set,
            address_line=payload.address_line,
            address_line_set="address_line" in fields_set,
        )
    except repo.DuplicateExternalIdError as exc:
        raise HTTPException(status_code=409, detail={"code": "duplicate_external_member_id", "value": str(exc)})
    except repo.DuplicateContactError as exc:
        raise HTTPException(status_code=409, detail={"code": "duplicate_contact", "value": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_contact", "detail": str(exc)})
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "customer_not_found"})
    return _to_profile(data)


@router.delete("/customers/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_customer(customer_id: UUID,
                          user: CurrentUser = Depends(get_current_user),
                          pool=Depends(get_pool)):
    await require_customer_in_scope(pool, customer_id, user)
    hidden = await repo.soft_delete_customer(pool, customer_id)
    if not hidden:
        raise HTTPException(status_code=404, detail={"code": "customer_not_found"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
