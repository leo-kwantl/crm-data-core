"""Pydantic v2 schemas for PII-safe customer search. snake_case JSON 1:1 with rows.

POST /api/v1/customers/search takes identifiers in the JSON body (never URL).
At least one of phone / email / name / member_id is required.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


class SearchRequest(BaseModel):
    phone: str | None = Field(default=None, description="Full phone, any format; normalized to E.164")
    email: str | None = Field(default=None, description="Email; normalized to lowercase")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    member_id: str | None = Field(default=None, description="Internal member ID (M-000123) OR client POS external ID, exact match")

    @model_validator(mode="after")
    def _at_least_one(self) -> SearchRequest:
        if not any(
            (v or "").strip() if isinstance(v, str) else v is not None
            for v in (self.phone, self.email, self.name, self.member_id)
        ):
            raise ValueError("at least one of phone, email, name, member_id is required")
        return self


class SearchContact(BaseModel):
    id: str
    customer_id: str
    kind: str
    value_normalized: str
    value_display: str
    is_primary: bool
    created_at: datetime


class SearchResultItem(BaseModel):
    id: str
    member_id: str
    full_name: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    contacts: list[SearchContact] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class SearchResponse(BaseModel):
    data: list[SearchResultItem] = Field(default_factory=list)
    no_match: bool = False
