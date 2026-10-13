"""Membership-tier + loyalty schemas (Pydantic v2, snake_case 1:1).

MVP semantics: tiers are assigned MANUALLY via PUT /customers/:id/tier.
There is no points engine, no auto-upgrade, no expiry. tier_code on the
customer row is the single source of truth; the loyalty endpoint derives
spend stats live from orders (never stored).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class TierCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    rank: int = Field(ge=0)
    benefits_text: str | None = None
    # Explicit brand; None = global catalogue row. When omitted the session
    # brand applies (group-wide roles keep None = global). Cross-brand 403.
    brand_id: UUID | None = None


class TierUpdate(BaseModel):
    # code is immutable (customers.tier_code references it).
    name: str | None = Field(default=None, min_length=1, max_length=200)
    rank: int | None = Field(default=None, ge=0)
    benefits_text: str | None = None
    clear_benefits: bool = False


class TierOut(BaseModel):
    id: UUID
    code: str
    name: str
    rank: int
    benefits_text: str | None = None
    created_at: datetime


class TierAssign(BaseModel):
    # tier_code null = unassign (back to no tier).
    tier_code: str | None = Field(default=None, max_length=32)


class LoyaltyOut(BaseModel):
    customer_id: UUID
    member_id: str
    tier: TierOut | None = None
    order_count: int = 0
    total_spent_cents: int = 0
