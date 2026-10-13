"""Customer Pydantic v2 schemas. JSON is snake_case 1:1 with columns.

Normalization rules (mirror docs/conventions.md):
- phone -> E.164 via phonenumbers (default region HK), exact match only.
- email -> lowercase + trim, exact match only.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import phonenumbers
from pydantic import BaseModel, Field, field_validator

ContactKind = Literal["phone", "email"]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_phone(raw: str) -> str:
    """Return E.164 normalized phone or raise ValueError."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("phone value is required")
    try:
        num = phonenumbers.parse(text, "HK")
    except Exception:
        raise ValueError("invalid phone number")
    if not phonenumbers.is_possible_number(num):
        raise ValueError("invalid phone number")
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def normalize_email(raw: str) -> str:
    """Return lowercase+trim email or raise ValueError."""
    text = (raw or "").strip().lower()
    if not EMAIL_RE.match(text):
        raise ValueError("invalid email address")
    return text


def normalize_external_member_id(raw: str | None) -> str | None:
    """Trim client POS ids; blank/None stays NULL (unlinked)."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if len(text) > 64:
        raise ValueError("external_member_id must be 64 chars or fewer")
    return text


def normalize_contact(kind: str, value: str) -> str:
    if kind == "phone":
        return normalize_phone(value)
    if kind == "email":
        return normalize_email(value)
    raise ValueError("kind must be phone|email")


class ContactIn(BaseModel):
    kind: ContactKind
    value: str = Field(min_length=1, max_length=320)
    is_primary: bool = False

    @field_validator("value")
    @classmethod
    def _validate_value(cls, v: str, info) -> str:
        kind = (info.data or {}).get("kind")
        if kind in ("phone", "email"):
            normalize_contact(kind, v)  # raises ValueError -> 422
        return v


class ContactOut(BaseModel):
    id: UUID
    customer_id: UUID
    kind: str
    value_normalized: str
    value_display: str
    is_primary: bool
    created_at: datetime


class CustomerCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    contacts: list[ContactIn] = Field(min_length=1)
    attributes: dict[str, Any] = Field(default_factory=dict)
    external_member_id: str | None = Field(default=None, max_length=64)

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("full_name is required")
        return v

    @field_validator("external_member_id")
    @classmethod
    def _clean_external_id(cls, v: str | None) -> str | None:
        return normalize_external_member_id(v)


class CustomerUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    attributes: dict[str, Any] | None = None
    contacts: list[ContactIn] | None = None
    # Present (even blank) -> update/clear; absent (None + not provided) -> untouched.
    # NOTE: None also means "not provided" here, so clearing uses "".
    external_member_id: str | None = Field(default=None, max_length=64)

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("full_name must not be blank")
        return v


class PurchaseSummary(BaseModel):
    order_count: int = 0
    total_spent_cents: int = 0
    last_order_at: datetime | None = None


class CustomerProfile(BaseModel):
    id: UUID
    member_id: str
    external_member_id: str | None = None
    full_name: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    contacts: list[ContactOut] = Field(default_factory=list)
    subscription_status: dict[str, str | None] = Field(default_factory=dict)
    preference_summary: dict[str, Any] = Field(default_factory=dict)
    purchase_summary: PurchaseSummary = Field(default_factory=PurchaseSummary)
    created_at: datetime
    updated_at: datetime


class CustomerListContact(BaseModel):
    """Masked contact copy for multi-row lists (never full PII)."""

    kind: str
    value_normalized: str
    value_display: str
    is_primary: bool


class CustomerListItem(BaseModel):
    """One row of GET /customers (global masked list, newest-first)."""

    id: UUID
    member_id: str
    external_member_id: str | None = None
    full_name: str
    masked_contact: str | None = None
    contacts: list[CustomerListContact] = Field(default_factory=list)
    order_count: int = 0
    sms_status: str | None = None
    contactable: bool = False
    created_at: datetime


class CustomerListResponse(BaseModel):
    data: list[CustomerListItem] = Field(default_factory=list)
    next_cursor: str | None = None
