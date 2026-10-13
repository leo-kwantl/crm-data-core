"""Pydantic v2 schemas for customer consent. snake_case JSON mirrors row shapes 1:1.

channel / status are free TEXT validated against the `channel` and
`consent_status` reference domains at the router (see repos/reference.py) —
no hardcoded enums here."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

# Free TEXT; the controlled vocabularies live in reference_values
# (domains `channel`, `consent_status`) and are enforced in routers/consents.py.
Channel = str
ConsentStatus = str


class ConsentItem(BaseModel):
    channel: Channel
    status: ConsentStatus
    captured_by: str
    captured_at: datetime


class ConsentUpdateItem(BaseModel):
    channel: Channel
    status: ConsentStatus
    captured_by: str | None = Field(default=None, description="Defaults to the session staff_code")


class ConsentStatement(BaseModel):
    consents: list[ConsentUpdateItem] = Field(min_length=1)


class ConsentListResponse(BaseModel):
    customer_id: UUID
    consents: list[ConsentItem]
    is_contactable_for_marketing: bool


class ConsentEventItem(BaseModel):
    id: UUID
    customer_id: UUID
    channel: Channel
    status: ConsentStatus
    captured_by: str
    captured_at: datetime


class ConsentHistoryResponse(BaseModel):
    customer_id: UUID
    events: list[ConsentEventItem]
