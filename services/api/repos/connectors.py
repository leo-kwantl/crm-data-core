"""Connector write repo (data D1): registry mutations + run enqueue.

D1 contract (`pipelines/ingest/connectors.py`, migration 019):
- validate-on-create via `validate_connector` (required per-type keys,
  no embedded secrets, reserved types rejected).
- `config` stores NON-SECRET params only; `secret_ref` names the env
  var holding the credential. The DB never stores a plaintext secret.
- `connector_sync_runs` is per-connector+entity run history (queued ->
  running -> succeeded/failed by the sync worker, out of band here).
- reads/sanitization for API surfaces: `to_public` echoes config (safe
  by construction) + `secret_ref` (a name, not a value) + `has_secret`
  presence bit. Secret VALUES never appear here.
"""
from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID

import asyncpg

from pipelines.ingest import connectors as C

SYNC_ENTITIES = ("customers", "orders", "events", "identity", "products")


class DuplicateConnectorError(ValueError):
    """(source_type, name) already registered."""


class ConnectorNotFoundError(LookupError):
    """No connector with that id."""


class ConnectorDisabledError(ValueError):
    """Manual trigger against a disabled connector (the kill-switch)."""


def has_secret(secret_ref: str | None) -> bool:
    """Presence bit for API surfaces: is the named env var currently set?"""
    return bool(secret_ref) and bool(os.environ.get(secret_ref))


def _config_to_dict(raw: Any) -> dict[str, Any]:
    # asyncpg returns jsonb as str; accept both str and dict.
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    return json.loads(raw)


def to_public(row: asyncpg.Record) -> dict[str, Any]:
    """API-safe connector shape: config + secret_ref + has_secret bit."""
    return {
        "id": row["id"],
        "source_type": row["source_type"],
        "name": row["name"],
        "config": _config_to_dict(row["config"]),
        "secret_ref": row["secret_ref"],
        "has_secret": has_secret(row["secret_ref"]),
        "enabled": row["enabled"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def create_connector(pool: asyncpg.Pool, source_type: str, name: str,
                           config: dict[str, Any] | None,
                           secret_ref: str | None,
                           enabled: bool = True,
                           created_by: str | None = None) -> dict[str, Any]:
    """Validate (D1 registry rules) + insert. Raises ValueError (422) or
    DuplicateConnectorError (409)."""
    rec = C.validate_connector(source_type, name, config, secret_ref)
    try:
        row = await pool.fetchrow(
            "INSERT INTO connectors"
            " (source_type, name, config, secret_ref, enabled, created_by)"
            " VALUES ($1, $2, $3::jsonb, $4, $5, $6)"
            " RETURNING *",
            rec["source_type"], rec["name"],
            json.dumps(rec["config"]),
            rec["secret_ref"], enabled, created_by)
    except asyncpg.UniqueViolationError:
        raise DuplicateConnectorError(
            f"connector '{rec['source_type']}/{rec['name']}'"
            " already registered")
    assert row is not None
    return to_public(row)


async def get_connector(pool: asyncpg.Pool,
                        connector_id: UUID) -> dict[str, Any]:
    """Fetch one connector (public shape). Raises ConnectorNotFoundError."""
    row = await pool.fetchrow(
        "SELECT * FROM connectors WHERE id = $1", connector_id)
    if row is None:
        raise ConnectorNotFoundError(f"connector {connector_id} not found")
    return to_public(row)


async def delete_connector(pool: asyncpg.Pool, connector_id: UUID) -> None:
    """Hard delete (sync-run history cascades). Raises
    ConnectorNotFoundError when missing."""
    res = await pool.execute(
        "DELETE FROM connectors WHERE id = $1", connector_id)
    if res == "DELETE 0":
        raise ConnectorNotFoundError(f"connector {connector_id} not found")


async def enqueue_sync(pool: asyncpg.Pool, connector_id: UUID, entity: str,
                       brand_id: UUID | None = None,
                       group_id: UUID | None = None) -> dict[str, Any]:
    """Manual trigger: validate + insert a `queued` run row (the sync
    worker picks it up out of band). Raises ConnectorNotFoundError,
    ConnectorDisabledError, or ValueError (bad entity)."""
    if entity not in SYNC_ENTITIES:
        raise ValueError(
            f"unknown entity '{entity}'"
            f" (expected one of {', '.join(SYNC_ENTITIES)})")
    row = await pool.fetchrow(
        "SELECT id, enabled FROM connectors WHERE id = $1", connector_id)
    if row is None:
        raise ConnectorNotFoundError(f"connector {connector_id} not found")
    if not row["enabled"]:
        raise ConnectorDisabledError(
            f"connector {connector_id} is disabled (kill-switch;"
            " re-enable before triggering)")
    run = await pool.fetchrow(
        "INSERT INTO connector_sync_runs"
        " (connector_id, entity, status, brand_id, group_id)"
        " VALUES ($1, $2, 'queued', $3, $4)"
        " RETURNING id, connector_id, entity, status, brand_id, group_id,"
        " rows_seen, rows_ok, rows_rejected, error, created_at",
        connector_id, entity, brand_id, group_id)
    assert run is not None
    return dict(run)


async def rotate_secret(pool: asyncpg.Pool, connector_id: UUID,
                        value: str) -> dict[str, Any]:
    """Credential rotation per the 019 contract (update env, no DDL).

    Sets the connector's `secret_ref` env var in the backend process and
    touches `updated_at`. The value is never persisted, logged, or
    echoed. Raises ConnectorNotFoundError, or ValueError when the
    connector has no `secret_ref` (nothing to rotate into).
    """
    row = await pool.fetchrow(
        "SELECT id, secret_ref FROM connectors WHERE id = $1", connector_id)
    if row is None:
        raise ConnectorNotFoundError(f"connector {connector_id} not found")
    if not row["secret_ref"]:
        raise ValueError(
            f"connector {connector_id} has no secret_ref"
            " (register one before rotating)")
    os.environ[row["secret_ref"]] = value
    upd = await pool.fetchrow(
        "UPDATE connectors SET updated_at = now() WHERE id = $1"
        " RETURNING *",
        connector_id)
    assert upd is not None
    return to_public(upd)
