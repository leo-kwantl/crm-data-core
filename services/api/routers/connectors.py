"""Connector write/trigger endpoints (data D1, admin-only).

All five mutations are admin-gated (require_admin — same precedent as
uploads: these move secrets + execution). Reads stay app-side.

- POST /connectors (create, validate-on-create, 409 on duplicate)
- DELETE /connectors/{id} (204; sync-run history cascades)
- POST /connectors/{id}/sync (manual trigger -> 202 queued run row;
  422 on disabled connector = the kill-switch, no force flag)
- POST /connectors/{id}/test (connection check, no persistence:
  config-shape validation + secret presence; stored or prospective)
- PUT /connectors/{id}/secret (credential rotation: sets the named env
  var at runtime per the 019 contract, touches updated_at, no DDL)

Secrets never round-trip: every response carries `has_secret`
(presence bit) only. The rotation value is never logged or echoed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Response, status

from ..deps import get_pool, require_admin
from ..repos import connectors as repo
from ..schemas.connector import (
    ConnectorCreate,
    ConnectorSecretRotate,
    ConnectorSyncTrigger,
    ConnectorTestRequest,
)
from pipelines.ingest import connectors as C

router = APIRouter(tags=["connectors"])

Admin = Annotated[object, Depends(require_admin)]
Pool = Annotated[asyncpg.Pool, Depends(get_pool)]


async def _guard(call: Callable[[], Awaitable[Any]]) -> Any:
    """Await a repo call, mapping domain errors to HTTP codes."""
    try:
        return await call()
    except repo.ConnectorNotFoundError as exc:
        raise HTTPException(status_code=404,
                            detail={"code": "not_found",
                                    "message": str(exc)})
    except repo.DuplicateConnectorError as exc:
        raise HTTPException(status_code=409,
                            detail={"code": "duplicate_connector",
                                    "message": str(exc)})
    except (ValueError, repo.ConnectorDisabledError) as exc:
        raise HTTPException(status_code=422,
                            detail={"code": "invalid_connector",
                                    "message": str(exc)})


@router.post("/connectors", status_code=status.HTTP_201_CREATED)
async def create_connector(body: ConnectorCreate, _admin: Admin,
                           pool: Pool) -> dict:
    return await _guard(lambda: repo.create_connector(
        pool, body.source_type, body.name, body.config,
        body.secret_ref, body.enabled))


@router.delete("/connectors/{connector_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector(connector_id: UUID, _admin: Admin,
                           pool: Pool) -> Response:
    await _guard(lambda: repo.delete_connector(pool, connector_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/connectors/{connector_id}/sync",
             status_code=status.HTTP_202_ACCEPTED)
async def trigger_sync(connector_id: UUID, body: ConnectorSyncTrigger,
                       _admin: Admin, pool: Pool) -> dict:
    run = await _guard(lambda: repo.enqueue_sync(
        pool, connector_id, body.entity, body.brand_id, body.group_id))
    return {"run_id": run["id"], "status": run["status"],
            "connector_id": run["connector_id"], "entity": run["entity"]}


@router.post("/connectors/{connector_id}/test")
async def test_connector(_admin: Admin, pool: Pool,
                         connector_id: UUID,
                         body: ConnectorTestRequest | None = None) -> dict:
    stored = await _guard(
        lambda: repo.get_connector(pool, connector_id))
    req = body or ConnectorTestRequest()
    source_type = req.source_type or stored["source_type"]
    config = dict(req.config) if req.config is not None else stored["config"]
    secret_ref = (req.secret_ref if req.secret_ref is not None
                  else stored["secret_ref"])
    try:
        C.validate_connector(source_type, stored["name"], config,
                             secret_ref)
        config_valid: bool = True
        config_error: str | None = None
    except ValueError as exc:
        config_valid = False
        config_error = str(exc)
    present = repo.has_secret(secret_ref)
    return {"connector_id": stored["id"],
            "source_type": source_type,
            "config_valid": config_valid,
            "config_error": config_error,
            "secret_ref": secret_ref,
            "secret_present": present,
            "has_secret": present}


@router.put("/connectors/{connector_id}/secret")
async def rotate_secret(connector_id: UUID, body: ConnectorSecretRotate,
                        _admin: Admin, pool: Pool) -> dict:
    upd = await _guard(lambda: repo.rotate_secret(
        pool, connector_id, body.value))
    return {"id": upd["id"], "secret_ref": upd["secret_ref"],
            "has_secret": upd["has_secret"],
            "updated_at": upd["updated_at"]}
