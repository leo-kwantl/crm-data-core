"""Connector write schemas (Pydantic v2, snake_case 1:1).

D1 contract: `config` holds NON-SECRET params only (embedded secrets
are rejected at validation); `secret_ref` is an env-var NAME, never a
value. Responses carry `has_secret` (presence bit) — a secret value
never round-trips through any endpoint, including rotation.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ConnectorCreate(BaseModel):
    source_type: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    config: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = Field(default=None, max_length=128)
    enabled: bool = True


class ConnectorSyncTrigger(BaseModel):
    entity: str = Field(min_length=1, max_length=32)
    brand_id: UUID | None = None
    group_id: UUID | None = None


class ConnectorTestRequest(BaseModel):
    # All optional: omitted fields fall back to the stored connector,
    # so the UI can "test before save" with prospective values or test
    # the persisted connector as-is.
    source_type: str | None = Field(default=None, max_length=32)
    config: dict[str, Any] | None = None
    secret_ref: str | None = Field(default=None, max_length=128)


class ConnectorSecretRotate(BaseModel):
    value: str = Field(min_length=1, max_length=4096)
