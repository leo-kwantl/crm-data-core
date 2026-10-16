"""Customer Pydantic v2 schemas. JSON is snake_case 1:1 with columns.

Normalization rules (mirror docs/conventions.md):
- phone -> E.164 via phonenumbers (default region HK), exact match only.
- email -> lowercase + trim, exact match only.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

import phonenumbers
from pydantic import BaseModel, Field, field_validator

ContactKind = Literal["phone", "email"]

Gender = Literal["female", "male", "other", "undisclosed"]
GENDERS = ("female", "male", "other", "undisclosed")


def normalize_gender(raw: str | None) -> str | None:
    """Trim gender vocabulary; blank/None stays NULL (unknown)."""
    if raw is None:
        return None
    text = raw.strip().lower()
    if not text:
        return None
    if text not in GENDERS:
        raise ValueError("gender must be female|male|other|undisclosed")
    return text


def normalize_date_of_birth(raw: date | str | None) -> date | None:
    """Accept a date or ISO string; reject future dates. Blank stays NULL."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        try:
            raw = date.fromisoformat(raw)
        except ValueError:
            raise ValueError("date_of_birth must be YYYY-MM-DD")
    if raw > date.today():
        raise ValueError("date_of_birth must not be in the future")
    return raw


def normalize_district(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if len(text) > 120:
        raise ValueError("district must be 120 chars or fewer")
    return text


def normalize_address_line(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if len(text) > 500:
        raise ValueError("address_line must be 500 chars or fewer")
    return text

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
    gender: str | None = None
    date_of_birth: date | str | None = None
    district: str | None = Field(default=None, max_length=120)
    address_line: str | None = Field(default=None, max_length=500)

    @field_validator("gender")
    @classmethod
    def _clean_gender(cls, v: str | None) -> str | None:
        return normalize_gender(v)

    @field_validator("date_of_birth")
    @classmethod
    def _clean_dob(cls, v: date | str | None) -> date | None:
        return normalize_date_of_birth(v)

    @field_validator("district")
    @classmethod
    def _clean_district(cls, v: str | None) -> str | None:
        return normalize_district(v)

    @field_validator("address_line")
    @classmethod
    def _clean_address(cls, v: str | None) -> str | None:
        return normalize_address_line(v)

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
    gender: str | None = None
    date_of_birth: date | str | None = None
    district: str | None = Field(default=None, max_length=120)
    address_line: str | None = Field(default=None, max_length=500)

    @field_validator("gender")
    @classmethod
    def _clean_gender(cls, v: str | None) -> str | None:
        return normalize_gender(v)

    @field_validator("date_of_birth")
    @classmethod
    def _clean_dob(cls, v: date | str | None) -> date | None:
        return normalize_date_of_birth(v)

    @field_validator("district")
    @classmethod
    def _clean_district(cls, v: str | None) -> str | None:
        return normalize_district(v)

    @field_validator("address_line")
    @classmethod
    def _clean_address(cls, v: str | None) -> str | None:
        return normalize_address_line(v)

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
    gender: str | None = None
    date_of_birth: date | None = None
    age_years: int | None = None
    district: str | None = None
    address_line: str | None = None
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
    gender: str | None = None
    age_years: int | None = None
    masked_contact: str | None = None
    contacts: list[CustomerListContact] = Field(default_factory=list)
    order_count: int = 0
    sms_status: str | None = None
    contactable: bool = False
    created_at: datetime


class CustomerListResponse(BaseModel):
    data: list[CustomerListItem] = Field(default_factory=list)
    next_cursor: str | None = None
