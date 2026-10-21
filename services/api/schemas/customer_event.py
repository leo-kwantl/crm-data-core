"""Pydantic v2 schemas for customer journey events. snake_case JSON mirrors row shapes 1:1.

kind is free TEXT validated against the `event_kind` / `event_source`
reference domains in routers/customer_events.py — no enum here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


def _non_blank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name}_must_not_be_blank")
    return value.strip()


class CustomerEventCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    source: str = Field(default="manual", min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=2000)
    payload: dict[str, Any] = Field(default_factory=dict)
    # No created_by on the way in: the producer is the acting session
    # (see deps.producer_of), so a client cannot write a row that names
    # somebody else. The response still carries created_by.

    @field_validator("kind")
    @classmethod
    def _kind_non_blank(cls, v: str) -> str:
        return _non_blank(v, "kind")

    @field_validator("source")
    @classmethod
    def _source_non_blank(cls, v: str) -> str:
        return _non_blank(v, "source")

    @field_validator("summary")
    @classmethod
    def _summary_non_blank(cls, v: str) -> str:
        return _non_blank(v, "summary")


class CustomerEventItem(BaseModel):
    id: UUID
    customer_id: UUID
    occurred_at: datetime
    kind: str
    source: str
    summary: str
    payload: dict[str, Any]
    created_by: str | None
    created_at: datetime


class CustomerEventListResponse(BaseModel):
    customer_id: UUID
    customer_member_id: str
    customer_name: str
    data: list[CustomerEventItem]
    next_cursor: str | None
