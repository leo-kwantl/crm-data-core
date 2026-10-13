"""Group/brand schemas (Pydantic v2, snake_case 1:1).

Hierarchy: groups -> brands -> stores. Single-brand clients = one group +
one brand (invisible to operators).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class GroupCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)


class GroupOut(BaseModel):
    id: UUID
    code: str
    name: str
    created_at: datetime
    brand_count: int = 0


class BrandCreate(BaseModel):
    group_id: UUID
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)


class BrandOut(BaseModel):
    id: UUID
    group_id: UUID
    code: str
    name: str
    created_at: datetime
    group_code: str | None = None
    group_name: str | None = None
    store_count: int = 0
