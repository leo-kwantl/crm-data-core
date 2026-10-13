"""Store schemas: CRUD with contact columns (Pydantic v2, snake_case 1:1)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class StoreCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    address: str | None = None
    phone: str | None = Field(default=None, max_length=64)
    # Explicit brand; when omitted the session brand applies (group-wide
    # roles fall back to the default DEMO brand). Cross-brand values 403.
    brand_id: UUID | None = None


class StoreUpdate(BaseModel):
    # code is immutable after create (orders/joins key off it in the UI).
    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = None
    clear_address: bool = False
    phone: str | None = Field(default=None, max_length=64)
    clear_phone: bool = False


class StoreOut(BaseModel):
    id: UUID
    code: str
    name: str
    address: str | None = None
    phone: str | None = None
    brand_id: UUID | None = None
    brand_code: str | None = None
    created_at: datetime


class StoreDetailOut(StoreOut):
    order_count: int = 0


class StoreListResponse(BaseModel):
    data: list[StoreOut] = Field(default_factory=list)
    next_cursor: str | None = None
