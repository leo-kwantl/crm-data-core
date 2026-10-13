"""Reference-data endpoints (auto-mounted by app.py discovery).

- GET /reference/domains — all vocabulary domains.
- GET /reference?domain=<d> — active codes for one domain, ranked.
"""

from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import CurrentUser, get_current_user, get_pool
from ..repos import reference as repo

router = APIRouter(tags=["reference"])


@router.get("/reference/domains")
async def get_domains(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = user
    async with pool.acquire() as conn:
        return {"domains": await repo.list_domains(conn)}


@router.get("/reference")
async def get_values(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    domain: Annotated[str, Query(min_length=1)],
) -> dict:
    _ = user
    async with pool.acquire() as conn:
        if not await repo.domain_exists(conn, domain):
            raise HTTPException(
                status_code=404,
                detail={"code": "unknown_domain", "domain": domain},
            )
        return {"domain": domain, "values": await repo.list_values(conn, domain)}
