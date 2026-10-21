"""Product catalogue endpoints (auto-mounted by app.py discovery).

- GET /products?q=&limit=&cursor= — name/sku search (non-PII, LIKE allowed).
  Needs a staff session (see the note below on why non-PII is not public).
- GET /products/{id} — single product.
- POST /products — create a catalogue row (201; 409 duplicate_sku).
- PATCH /products/{id} — edit name/category/price_cents plus enrichment
  fields (description, specs, image_url http(s), is_active); sku immutable.
  Explicit null clears description/image_url, resets specs to {}.
- PUT + DELETE -> 405 (no full-replace, no product delete).

The reads require a session too (review §7.2-3). The catalogue is not PII,
but an unauthenticated `/products` still enumerates the store's commercial
surface (names, skus, prices) for anyone who can reach the API, and the
session dependency is the only thing that makes a route auditable at all.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from ..deps import CurrentUser, get_current_user, get_pool, session_brand
from ..repos import products as repo
from ..repos.paging import clamp_page_size
from ..schemas.product import ProductCreate, ProductUpdate

router = APIRouter(tags=["products"])


@router.get("/products")
async def list_products(
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    user: Annotated[CurrentUser, Depends(get_current_user)],
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int, Query()] = 20,
    category: Annotated[str | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> dict:
    """Catalogue list, newest-first. Optional additive filters: ?category=
    (exact match) + ?is_active=true|false. Cursor + offset modes honour both."""
    size = clamp_page_size(page_size)
    brand = session_brand(user)
    async with pool.acquire() as conn:
        if page is not None:
            return await repo.list_products_offset(
                conn, q=q, page=page, page_size=size,
                category=category, is_active=is_active,
                brand_id=brand,
            )
        try:
            rows, next_cursor = await repo.list_products(
                conn, q=q, limit=limit, cursor=cursor,
                category=category, is_active=is_active,
                brand_id=brand,
            )
        except ValueError:
            raise HTTPException(status_code=400, detail={"code": "invalid_cursor"})
    return {"data": rows, "next_cursor": next_cursor}


@router.get("/products/{product_id}")
async def get_product(
    product_id: uuid.UUID,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    async with pool.acquire() as conn:
        product = await repo.get_product(conn, product_id, session_brand(user))
    if product is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return product


@router.post("/products", status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreate,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    async with pool.acquire() as conn:
        try:
            return await repo.create_product(
                conn,
                sku=payload.sku,
                name=payload.name,
                category=payload.category,
                price_cents=payload.price_cents,
            )
        except repo.DuplicateSkuError as exc:
            raise HTTPException(
                status_code=409, detail={"code": "duplicate_sku", "value": str(exc)}
            )


@router.patch("/products/{product_id}")
async def patch_product(
    product_id: uuid.UUID,
    payload: ProductUpdate,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    provided = payload.model_fields_set
    async with pool.acquire() as conn:
        product = await repo.update_product(
            conn,
            product_id,
            name=payload.name,
            category=payload.category,
            price_cents=payload.price_cents,
            description=payload.description,
            description_set="description" in provided,
            specs=payload.specs,
            specs_set="specs" in provided,
            image_url=payload.image_url,
            image_url_set="image_url" in provided,
            is_active=payload.is_active,
        )
    if product is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return product


async def _method_not_allowed() -> Response:
    return Response(status_code=405)


for _path in ("/products", "/products/{product_id}"):
    for _method in ("PUT", "DELETE"):
        router.add_api_route(
            _path,
            _method_not_allowed,
            methods=[_method],
            include_in_schema=False,
        )
