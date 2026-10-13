from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


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
