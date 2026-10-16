"""Dashboard stats endpoint (auto-mounted, read-only).

GET /stats/overview[?granularity=week|month|year&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD]
— totals + monthly series for charts, plus opt-in grain series.
Aggregate-only, no customer rows, so no PII handling beyond the standard
session guard. Default shape is frozen (6 monthly buckets); granularity
or date params add revenue_series/new_customers_series without changing
existing keys. Totals are scope-filtered lifetime (HQ sees global);
series respect the filter. active_campaigns is global reference data
(campaigns carry no brand scope).
"""

from __future__ import annotations

import datetime as _dt
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from ..analytics.periods import parse_mode
from ..deps import CurrentUser, get_current_user, get_pool, scope_filters
from ..repos import stats as repo

router = APIRouter(tags=["stats"])

_GRANULARITIES = ("week", "month", "year")


@router.get("/stats/overview")
async def overview(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    granularity: Annotated[str, Query()] = "month",
    date_from: Annotated[_dt.date | None, Query()] = None,
    date_to: Annotated[_dt.date | None, Query()] = None,
    mode: Annotated[str, Query()] = "latest",
) -> dict:
    _ = user
    brand_id, group_id = scope_filters(user)
    if granularity not in _GRANULARITIES:
        raise HTTPException(status_code=422, detail={
            "code": "bad_granularity", "value": granularity})
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail={"code": "bad_date_range"})
    mode = parse_mode(mode)
    return await repo.get_overview(pool, granularity=granularity,
                                   date_from=date_from, date_to=date_to,
                                   brand_id=brand_id, group_id=group_id,
                                   mode=mode)
