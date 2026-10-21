"""Inventory endpoints: per-store stock levels (auto-mounted).

- GET /inventory?product_id= — stock rows for one product (store_code order).
  product_id is required; unknown product -> 404.
- PUT /inventory {product_id, store_id, qty} — upsert the pair row (200).
  Unknown product/store -> 404; negative qty -> 422 (schema + CHECK).

Non-PII resource like the catalogue, but not a public one: both reads and
the PUT upsert require a staff session. Stock levels per store are
operational data (what is where, and how much), and review §7.2-3 found
`GET /inventory` answering anonymously.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import CurrentUser, check_brand, get_current_user, get_pool, session_brand
from ..repos import inventory as repo
from ..schemas.inventory import InventoryUpsert

router = APIRouter(tags=["inventory"])


@router.get("/inventory")
async def list_inventory(
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    product_id: Annotated[uuid.UUID, Query()],
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    async with pool.acquire() as conn:
        rows = await repo.list_inventory(conn, product_id, session_brand(user))
    if rows is None:
        raise HTTPException(status_code=404, detail={"code": "product_not_found"})
    return {"data": rows}


@router.put("/inventory")
async def upsert_inventory(
    payload: InventoryUpsert,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    async with pool.acquire() as conn:
        # A stock row lands in a STORE, and a store belongs to a brand: a
        # brand-scoped session may not write another brand's shelf.
        store_brand = await repo.store_brand_id(conn, payload.store_id)
        check_brand(user, store_brand)
        result = await repo.upsert_inventory(
            conn,
            product_id=payload.product_id,
            store_id=payload.store_id,
            qty=payload.qty,
        )
    if isinstance(result, tuple):
        _, code = result
        raise HTTPException(status_code=404, detail={"code": code})
    return result
