"""Campaign schemas (Pydantic v2, snake_case 1:1).

Status machine: draft -> active -> ended. Same-status PATCH is a no-op
(allowed); any other jump is rejected with 422 illegal_transition.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CampaignCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="promotion", max_length=32)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    # Explicit brand; when omitted the session brand applies (group-wide
    # roles fall back to the default DEMO brand). Cross-brand values 403.
    brand_id: UUID | None = None


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: str | None = None
    starts_at: datetime | None = None
    clear_starts_at: bool = False
    ends_at: datetime | None = None
    clear_ends_at: bool = False
    config: dict[str, Any] | None = None


class CampaignOut(BaseModel):
    id: UUID
    code: str
    name: str
    kind: str
    status: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class CampaignMemberAdd(BaseModel):
    customer_id: UUID


class CampaignMemberOut(BaseModel):
    customer_id: UUID
    member_id: str
    full_name: str
    assigned_by: str
    assigned_at: datetime
