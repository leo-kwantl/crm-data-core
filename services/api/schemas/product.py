from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


def _clean_text(raw: str, field: str, max_len: int) -> str:
    text = (raw or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_len:
        raise ValueError(f"{field} must be {max_len} chars or fewer")
    return text


class Product(BaseModel):
    """Product row. JSON snake_case 1:1 with products columns (migration 008)."""

    id: UUID
    sku: str
    name: str
    category: str
    price_cents: int
    description: str | None = None
    specs: dict[str, Any] = Field(default_factory=dict)
    image_url: str | None = None
    is_active: bool = True
    created_at: datetime


class ProductCreate(BaseModel):
    """New catalogue row. sku is immutable once created (no sku PATCH)."""

    sku: str
    name: str
    category: str
    price_cents: int = Field(ge=0)

    @field_validator("sku")
    @classmethod
    def _clean_sku(cls, v: str) -> str:
        return _clean_text(v, "sku", 64)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        return _clean_text(v, "name", 200)

    @field_validator("category")
    @classmethod
    def _clean_category(cls, v: str) -> str:
        return _clean_text(v, "category", 64)


class ProductUpdate(BaseModel):
    """Partial edit (sku immutable). Enrichment fields mirror migration 008:
    description / image_url accept explicit null to clear; specs accepts
    explicit null to reset to {}. image_url must be http(s) when set."""

    name: str | None = None
    category: str | None = None
    price_cents: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=4000)
    specs: dict[str, Any] | None = None
    image_url: str | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str | None) -> str | None:
        return None if v is None else _clean_text(v, "name", 200)

    @field_validator("category")
    @classmethod
    def _clean_category(cls, v: str | None) -> str | None:
        return None if v is None else _clean_text(v, "category", 64)

    @field_validator("image_url")
    @classmethod
    def _http_image_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        text = v.strip()
        if not text.startswith(("http://", "https://")):
            raise ValueError("image_url must be an http(s) URL")
        if len(text) > 2048:
            raise ValueError("image_url must be 2048 chars or fewer")
        return text

    @model_validator(mode="after")
    def _at_least_one(self) -> ProductUpdate:
        # Explicit null counts as provided (it clears the field).
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class ProductListResponse(BaseModel):
    data: list[Product]
    next_cursor: str | None = None
