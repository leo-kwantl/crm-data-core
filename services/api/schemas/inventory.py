from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class InventoryUpsert(BaseModel):
    """Replace the stock qty for one (product, store) pair (upsert)."""

    product_id: UUID
    store_id: UUID
    qty: int = Field(ge=0)
