"""Consent endpoints: GET + PUT /customers/{id}/consents (full statement)
plus GET /customers/{id}/consents/history (append-only audit trail).

PII: session identity (x-session-token); Cache-Control: no-store applied by
global middleware (all /api/v1/customers/* paths); never log request bodies.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from ..deps import CurrentUser, get_current_user, get_pool, require_customer_in_scope
from ..repos import consents as repo
from ..repos import groups as groups_repo
from ..repos import reference as refrepo
from ..schemas.consent import (
    ConsentHistoryResponse,
    ConsentListResponse,
    ConsentStatement,
)

router = APIRouter(tags=["consents"])


def _not_found(customer_id: UUID) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"code": "customer_not_found", "customer_id": str(customer_id)}
    )


@router.get("/customers/{customer_id}/consents", response_model=ConsentListResponse)
async def read_consents(
    customer_id: UUID,
    pool=Depends(get_pool),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    await require_customer_in_scope(pool, customer_id, user)
    async with pool.acquire() as conn:
        if not await repo.customer_exists(conn, customer_id):
            raise _not_found(customer_id)
        rows = await repo.get_consents(conn, customer_id)
    return {
        "customer_id": customer_id,
        "consents": rows,
        "is_contactable_for_marketing": repo.derive_contactability(rows),
    }


@router.put("/customers/{customer_id}/consents", response_model=ConsentListResponse)
async def replace_consents(
    customer_id: UUID,
    statement: ConsentStatement,
    pool=Depends(get_pool),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    seen: set[str] = set()
    for item in statement.consents:
        if item.channel in seen:
            raise HTTPException(
                status_code=422, detail={"code": "duplicate_channel", "channel": item.channel}
            )
        seen.add(item.channel)
    async with pool.acquire() as conn:
        for item in statement.consents:
            for domain, code in (
                ("channel", item.channel),
                ("consent_status", item.status),
            ):
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
    await require_customer_in_scope(pool, customer_id, user)
    brand_id = await groups_repo.resolve_write_brand(
        pool, explicit=None, session_brand_id=user.brand_id
    )
    try:
        rows = await repo.replace_consents(
            pool,
            customer_id,
            [item.model_dump() for item in statement.consents],
            default_captured_by=user.staff_code,
            brand_id=brand_id,
        )
    except KeyError:
        raise _not_found(customer_id)
    return {
        "customer_id": customer_id,
        "consents": rows,
        "is_contactable_for_marketing": repo.derive_contactability(rows),
    }


@router.get(
    "/customers/{customer_id}/consents/history", response_model=ConsentHistoryResponse
)
async def read_consent_history(
    customer_id: UUID,
    pool=Depends(get_pool),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    await require_customer_in_scope(pool, customer_id, user)
    async with pool.acquire() as conn:
        if not await repo.customer_exists(conn, customer_id):
            raise _not_found(customer_id)
        events = await repo.get_consent_events(conn, customer_id)
    return {"customer_id": customer_id, "events": events}


