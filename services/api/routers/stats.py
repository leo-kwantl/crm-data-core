"""Dashboard stats endpoint (auto-mounted, read-only).

GET /stats/overview — totals + monthly series for charts. Aggregate-only,
no customer rows, so no PII handling beyond the standard session guard.
"""

from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends

from ..deps import CurrentUser, get_current_user, get_pool
from ..repos import stats as repo

router = APIRouter(tags=["stats"])


@router.get("/stats/overview")
async def overview(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = user
    return await repo.get_overview(pool)
