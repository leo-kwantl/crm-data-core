"""Customer profile routes: POST/GET/PATCH/DELETE.

Cache-Control: no-store is applied by middleware (see deps.no_store_headers).
Never log request bodies here (PII rule).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from ..deps import get_pool
from ..repos import customers as repo
from ..schemas.customer import CustomerCreate, CustomerProfile, CustomerUpdate

router = APIRouter(tags=["customers"])


def _to_profile(data: dict) -> CustomerProfile:
    return CustomerProfile(**data)


@router.post("/customers", response_model=CustomerProfile, status_code=status.HTTP_201_CREATED)
async def create_customer(payload: CustomerCreate, pool=Depends(get_pool)):
    try:
        data = await repo.create_customer(
            pool,
            full_name=payload.full_name,
            contacts=[c.model_dump() for c in payload.contacts],
            attributes=payload.attributes,
            external_member_id=payload.external_member_id,
        )
    except repo.DuplicateExternalIdError as exc:
        raise HTTPException(status_code=409, detail={"code": "duplicate_external_member_id", "value": str(exc)})
    except repo.DuplicateContactError as exc:
        raise HTTPException(status_code=409, detail={"code": "duplicate_contact", "value": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_contact", "detail": str(exc)})
    return _to_profile(data)


@router.get("/customers/{customer_id}", response_model=CustomerProfile)
async def read_customer(customer_id: UUID, pool=Depends(get_pool)):
    data = await repo.get_customer(pool, customer_id)
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "customer_not_found"})
    return _to_profile(data)


@router.patch("/customers/{customer_id}", response_model=CustomerProfile)
async def patch_customer(customer_id: UUID, payload: CustomerUpdate, pool=Depends(get_pool)):
    try:
        data = await repo.update_customer(
            pool,
            customer_id,
            full_name=payload.full_name,
            attributes=payload.attributes,
            contacts=[c.model_dump() for c in payload.contacts] if payload.contacts is not None else None,
            external_member_id=payload.external_member_id,
            external_member_id_set="external_member_id" in payload.model_fields_set,
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
async def delete_customer(customer_id: UUID, pool=Depends(get_pool)):
    hidden = await repo.soft_delete_customer(pool, customer_id)
    if not hidden:
        raise HTTPException(status_code=404, detail={"code": "customer_not_found"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
