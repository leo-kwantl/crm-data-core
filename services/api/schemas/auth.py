"""Password-login schemas (Pydantic v2). JSON is snake_case 1:1."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    identifier: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class BrandContext(BaseModel):
    brand_id: str | None = None
    brand_code: str | None = None
    brand_name: str | None = None
    group_id: str | None = None
    group_code: str | None = None
    group_name: str | None = None


class LoginOut(BrandContext):
    token: str
    staff_code: str
    role: str
    user_id: str


class MeOut(BrandContext):
    user_id: str
    username: str
    staff_code: str
    role: str


class PasswordChangeIn(BaseModel):
    current: str = Field(min_length=1, max_length=128)
    new: str = Field(min_length=1, max_length=128)


class PasswordSetIn(BaseModel):
    new: str = Field(min_length=1, max_length=128)
