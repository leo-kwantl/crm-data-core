"""PII-safe customer search endpoints.

- POST /customers/search — phone/email/name/member_id in JSON body (never URL).
- GET /customers?member_id= — direct fetch by low-sensitivity counter ID.
  phone/email as URL query params are rejected (422, code pii_in_url).

The UUID fetch (GET /customers/{customer_id}) is the customers router's
route, not this one: two routes for one path means the first registered one
answers and the second never runs, so the audit and the rate limit live
where the answer does (routers/customers.py). The receipt is
services/api/tests/test_customer_fetch_audit.py.

Every call is rate-limited per staff identity and appends one audit row with
criteria_type + result_count (never raw PII values). Cache-Control: no-store
is applied by global middleware; request bodies are never logged.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..deps import (
    CurrentUser,
    check_search_rate_limit,
    get_current_user,
    get_pool,
    session_brand,
)
from ..repos import customers as customers_repo
from ..repos import search as repo
from ..repos.paging import clamp_page_size
from ..schemas.customer import CustomerListResponse
from ..schemas.search import SearchRequest

router = APIRouter(tags=["search"])

# Tunable via monkeypatch in tests; production default 60 searches/min/staff.
RATE_LIMIT = 60
RATE_WINDOW_S = 60


def _limit(request: Request, staff_id: str, route: str) -> None:
    check_search_rate_limit(
        staff_id, limit=RATE_LIMIT, window_s=RATE_WINDOW_S, route=f"search:{route}"
    )


def _bad_phone_email(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "invalid_identifier", "error": str(exc)})


@router.post("/customers/search")
async def post_search(
    body: SearchRequest,
    request: Request,
    pool=Depends(get_pool),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _limit(request, user.staff_code, "post")
    async with pool.acquire() as conn:
        try:
            items, criteria_type = await repo.search(
                conn, phone=body.phone, email=body.email,
                name=body.name, member_id=body.member_id,
                group_id=user.group_id, brand_id=session_brand(user),
            )
        except ValueError as exc:
            raise _bad_phone_email(exc)
        first_id = UUID(items[0]["id"]) if items else None
        await repo.write_audit(
            conn, staff_id=user.staff_code, customer_id=first_id,
            criteria_type=criteria_type, result_count=len(items),
        )
    return {"data": items, "no_match": len(items) == 0}


@router.get("/customers")
async def get_by_member_id(
    request: Request,
    member_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    page_size: int = Query(default=20),
    tier_code: str | None = Query(default=None),
    sms_status: str | None = Query(default=None),
    pool=Depends(get_pool),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    # Reject PII in URL: any phone/email query param must never trigger a lookup.
    qp = request.query_params
    if "phone" in qp or "email" in qp:
        raise HTTPException(status_code=422, detail={"code": "pii_in_url"})
    if not member_id or not member_id.strip():
        # Global masked list (portal): newest-first, cursor-paginated.
        # Added additively; the member_id lookup below is unchanged.
        # Optional filters: ?tier_code= (exact, upper-cased) +
        # ?sms_status=opted_in|opted_out (400 on any other value).
        _limit(request, user.staff_code, "list")
        size = clamp_page_size(page_size)
        try:
            tier, sms = customers_repo.normalize_filters(tier_code, sms_status)
        except ValueError:
            raise HTTPException(status_code=400, detail={"code": "invalid_sms_status"})
        if page is not None:
            # Numbered page (offset mode); cursor params stay valid but unused.
            async with pool.acquire() as conn:
                payload = await customers_repo.list_customers_offset(
                    conn, page=page, page_size=size,
                    tier_code=tier, sms_status=sms,
                    group_id=user.group_id, brand_id=session_brand(user),
                )
                await repo.write_audit(
                    conn, staff_id=user.staff_code, customer_id=None,
                    criteria_type="list", result_count=len(payload["data"]),
                )
            return payload
        async with pool.acquire() as conn:
            try:
                items, next_cursor = await customers_repo.list_customers_page(
                    conn, limit=limit, cursor=cursor,
                    tier_code=tier, sms_status=sms,
                    group_id=user.group_id, brand_id=session_brand(user),
                )
            except ValueError:
                raise HTTPException(status_code=400, detail={"code": "invalid_cursor"})
            await repo.write_audit(
                conn, staff_id=user.staff_code, customer_id=None,
                criteria_type="list", result_count=len(items),
            )
        try:
            # Validate shape against the list contract before returning.
            CustomerListResponse(data=items, next_cursor=next_cursor)
        except Exception as exc:  # pragma: no cover - programming error
            raise HTTPException(status_code=500, detail={"code": "list_shape"}) from exc
        return {"data": items, "next_cursor": next_cursor}
    _limit(request, user.staff_code, "member")
    async with pool.acquire() as conn:
        items, _ = await repo.search(conn, phone=None, email=None, name=None,
                                     member_id=member_id.strip(),
                                     group_id=user.group_id,
                                     brand_id=session_brand(user),
                                     member_partial=False)
        first_id = UUID(items[0]["id"]) if items else None
        await repo.write_audit(
            conn, staff_id=user.staff_code, customer_id=first_id,
            criteria_type="member_id", result_count=len(items),
        )
    return {"data": items, "no_match": len(items) == 0}

