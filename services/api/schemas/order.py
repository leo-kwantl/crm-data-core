from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field


class OrdersSearchRequest(BaseModel):
    """JSON body of POST /orders/search — the filters GET /orders accepts.

    The customer name (`q`) travels in the BODY, never in the URL: a name in
    a query string lands in browser history, the access log and the Referer
    header. Field names + semantics mirror the GET /orders query params.
    """

    q: str | None = Field(default=None, max_length=200)
    store_code: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    status: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None
    page: int | None = Field(default=None, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class OrderSummary(BaseModel):
    """One row of GET /customers/:id/orders. Column names 1:1 + item_count."""

    id: UUID
    customer_id: UUID
    store_id: UUID
    ordered_at: datetime
    total_cents: int
    status: str
    item_count: int


class OrderListResponse(BaseModel):
    data: list[OrderSummary]
    next_cursor: str | None = None


class OrderItem(BaseModel):
    """Line item with joined product display fields (product_name, sku)."""

    id: UUID
    order_id: UUID
    product_id: UUID
    qty: int
    unit_price_cents: int
    product_name: str
    sku: str


class OrderDetailResponse(BaseModel):
    """GET /orders/:id: header 1:1 with orders columns + store names + items."""

    id: UUID
    customer_id: UUID
    store_id: UUID
    ordered_at: datetime
    total_cents: int
    status: str
    store_code: str
    store_name: str
    items: list[OrderItem] = Field(default_factory=list)


class GlobalOrderSummary(OrderSummary):
    """One row of GET /orders (global list): OrderSummary + display fields."""

    customer_member_id: str
    customer_name: str
    store_code: str
    store_name: str


class GlobalOrderListResponse(BaseModel):
    data: list[GlobalOrderSummary] = Field(default_factory=list)
    next_cursor: str | None = None
