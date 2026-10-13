"""Identity schemas: staff roster + login-account records (Pydantic v2).

ACCOUNT RECORDS ONLY — no passwords, secrets, or sessions. Real login/auth is
done at pilot hardening: session auth via x-session-token (deps.py).
JSON is snake_case 1:1 with columns.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

STAFF_ROLES = ("admin", "manager", "sa")
USER_ROLES = ("admin", "manager", "sa")


class StaffCreate(BaseModel):
    staff_code: str = Field(min_length=1, max_length=32)
    full_name: str = Field(min_length=1, max_length=200)
    store_id: UUID | None = None
    role: str = Field(default="sa", max_length=32)
    # Brand scope link. When omitted the home store's brand applies; NULL =
    # group-wide (manager/admin only by convention). Unknown brand -> 422.
    brand_id: UUID | None = None


class StaffUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    store_id: UUID | None = None
    clear_store: bool = False
    role: str | None = Field(default=None, max_length=32)
    # Explicit null clears the link (back to group-wide). Presence — not
    # value — decides: the router detects it via model_fields_set.
    brand_id: UUID | None = None
    left_at: datetime | None = None


class StaffOut(BaseModel):
    id: UUID
    staff_code: str
    full_name: str
    store_id: UUID | None = None
    store_code: str | None = None
    role: str
    joined_at: datetime
    left_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class StaffListResponse(BaseModel):
    data: list[StaffOut] = Field(default_factory=list)
    next_cursor: str | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    role: str = Field(default="sa", max_length=32)
    staff_id: UUID | None = None


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=32)
    is_disabled: bool | None = None
    staff_id: UUID | None = None
    clear_staff: bool = False


class UserOut(BaseModel):
    id: UUID
    username: str
    display_name: str
    role: str
    is_disabled: bool = False
    staff_id: UUID | None = None
    staff_code: str | None = None
    created_at: datetime
    updated_at: datetime


class UserListResponse(BaseModel):
    data: list[UserOut] = Field(default_factory=list)
    next_cursor: str | None = None
