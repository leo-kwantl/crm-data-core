"""Connector write/trigger endpoints (data D1, admin-only).

Every mutation below is admin-gated (require_admin — same precedent as
uploads: these move secrets + execution). Reads stay app-side.

- POST /connectors (create, validate-on-create, 409 on duplicate)
- DELETE /connectors/{id} (204; sync-run history cascades)
- POST /connectors/{id}/sync (manual trigger -> 202 queued run row;
  422 on disabled connector = the kill-switch, no force flag)
- POST /connectors/{id}/test (connection check, no persistence:
  config-shape validation + secret presence; stored or prospective)
- PUT /connectors/{id}/secret (credential rotation: sets the named env
  var at runtime per the 019 contract, touches updated_at, no DDL)
- PUT /connectors/{id}/schedule (LD2 per-connector timetable: 5-field
  cron per the 025 contract, null clears to manual-only; the sync
  checkpoint last_sync_at/last_sync_status is worker-owned, read-only)

Secrets never round-trip: every response carries `has_secret`
(presence bit) only. The rotation value is never logged or echoed.

READS (mirrored verbatim from app `routers/connectors.py` so the
mirror is a single whole file — merge, not overwrite):
- GET /connectors (global registry + latest in-scope run each)
- GET /connectors/{id} (one connector + latest in-scope run)
- GET /connectors/{id}/sync-runs[?entity=&status=&limit=]
Staff-gated (any signed-in role); SQL-backed via db/queries/connectors.sql.
"""

from __future__ import annotations

import re
import uuid as uuid_mod
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from ..deps import (
    CurrentUser,
    get_current_user,
    get_pool,
    require_admin,
    scope_filters,
)
from ..repos import connectors as repo
from ..schemas.connector import (
    ConnectorCreate,
    ConnectorScheduleUpdate,
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
        # Aligned on 'connector_not_found' with the reads (app ruling
        # 2026-10-15) so the UI maps a single 404 code.
        raise HTTPException(status_code=404,
                            detail={"code": "connector_not_found",
                                    "message": str(exc)})
    except repo.DuplicateConnectorError as exc:
        raise HTTPException(status_code=409,
                            detail={"code": "duplicate_connector",
                                    "message": str(exc)})
    except repo.ConnectorDisabledError as exc:
        # Distinct code (not generic invalid_connector) so the UI can
        # render kill-switch-specific copy (app request 2026-10-15).
        raise HTTPException(status_code=422,
                            detail={"code": "connector_disabled",
                                    "message": str(exc)})
    except ValueError as exc:
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


@router.put("/connectors/{connector_id}/schedule")
async def update_schedule(connector_id: UUID, body: ConnectorScheduleUpdate,
                          _admin: Admin, pool: Pool) -> dict:
    """Set (or clear with null = manual-only) a connector's schedule.

    Admin-gated like the other connector mutations; the 5-field cron is
    validated per the 025 contract (bad timetables -> 422). No audit
    row: connector mutations carry no PII and none of the existing
    connector write endpoints audit. The sync checkpoint
    (last_sync_at/last_sync_status) is worker-owned and read-only here.
    """
    upd = await _guard(lambda: repo.update_schedule(
        pool, connector_id, body.schedule_cron))
    out = dict(upd)
    out["id"] = str(out["id"])
    out["created_at"] = out["created_at"].isoformat()
    out["updated_at"] = out["updated_at"].isoformat()
    out["last_sync_at"] = (out["last_sync_at"].isoformat()
                             if out["last_sync_at"] else None)
    return out


@router.put("/connectors/{connector_id}/secret")
async def rotate_secret(connector_id: UUID, body: ConnectorSecretRotate,
                        _admin: Admin, pool: Pool) -> dict:
    upd = await _guard(lambda: repo.rotate_secret(
        pool, connector_id, body.value))
    return {"id": upd["id"], "secret_ref": upd["secret_ref"],
            "has_secret": upd["has_secret"],
            "updated_at": upd["updated_at"]}


# ---- reads (app-mirrored verbatim; app owns the text) ----

QUERIES_PATH = (Path(__file__).resolve().parents[3] / "db" / "queries"
                / "connectors.sql")

_NAME_RE = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.M)


def _load_queries() -> dict[str, str]:
    text = QUERIES_PATH.read_text(encoding="utf-8")
    parts = _NAME_RE.split(text)
    out: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        out[parts[i]] = parts[i + 1].strip().rstrip(";")
    return out


Q = _load_queries()

ENTITIES = ("customers", "orders", "events", "identity", "products")
STATUSES = ("queued", "running", "succeeded", "failed")


def _connector_row(r: asyncpg.Record) -> dict:
    import json as _json
    import os as _os

    cfg = r["config"]
    # CONVERGED (app ruling 2026-10-15): has_secret = env-live, computed
    # Python-side from the stored secret_ref NAME (never a value).
    ref = r["secret_ref"]
    row = {"id": str(r["id"]),
            "source_type": r["source_type"],
            "name": r["name"],
            "config": dict(cfg) if isinstance(cfg, dict)
                      else _json.loads(cfg),
            "secret_ref": ref,
            "has_secret": bool(ref) and bool(_os.environ.get(ref)),
            "enabled": bool(r["enabled"]),
            "created_by": r["created_by"],
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
            # LD2 schedule + sync checkpoint (025; NULL = never
            # scheduled / never synced). last_sync_at renders ISO;
            # the UI formats it via lib/format (never raw).
            "schedule_cron": r["schedule_cron"],
            "last_sync_at": (r["last_sync_at"].isoformat()
                               if r["last_sync_at"] else None),
            "last_sync_status": r["last_sync_status"]}
    return row


def _run_row(r: asyncpg.Record) -> dict:
    return {"id": str(r["id"]),
            "entity": r["entity"],
            "status": r["status"],
            "rows_seen": int(r["rows_seen"]),
            "rows_ok": int(r["rows_ok"]),
            "rows_rejected": int(r["rows_rejected"]),
            "error": r["error"],
            "started_at": r["started_at"].isoformat()
                         if r["started_at"] else None,
            "finished_at": r["finished_at"].isoformat()
                          if r["finished_at"] else None,
            "created_at": r["created_at"].isoformat()}


async def _get_connector_row(conn, connector_id: str) -> asyncpg.Record:
    try:
        cid = uuid_mod.UUID(connector_id)
    except ValueError:
        raise HTTPException(status_code=422, detail={
            "code": "bad_connector_id", "value": connector_id})
    row = await conn.fetchrow(Q["connector_by_id"], cid)
    if row is None:
        raise HTTPException(status_code=404,
                            detail={"code": "connector_not_found"})
    return row


@router.get("/connectors")
async def list_connectors(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = user
    brand_id, group_id = scope_filters(user)
    async with pool.acquire() as conn:
        rows = await conn.fetch(Q["connector_list"])
        items = []
        for r in rows:
            last = await conn.fetchrow(Q["connector_last_run"], r["id"],
                                       brand_id, group_id)
            d = _connector_row(r)
            d["last_run"] = ({"status": last["status"],
                              "finished_at": last["finished_at"].isoformat()
                                             if last["finished_at"] else None}
                             if last else None)
            items.append(d)
    return {"connectors": items}


@router.get("/connectors/{connector_id}")
async def get_connector(
    connector_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict:
    _ = user
    brand_id, group_id = scope_filters(user)
    async with pool.acquire() as conn:
        row = await _get_connector_row(conn, connector_id)
        last = await conn.fetchrow(Q["connector_last_run"], row["id"],
                                   brand_id, group_id)
    d = _connector_row(row)
    d["last_run"] = ({"status": last["status"],
                      "finished_at": last["finished_at"].isoformat()
                                     if last["finished_at"] else None}
                     if last else None)
    return d


@router.get("/connectors/{connector_id}/sync-runs")
async def list_sync_runs(
    connector_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    entity: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict:
    if entity is not None and entity not in ENTITIES:
        raise HTTPException(status_code=422, detail={
            "code": "bad_entity", "value": entity})
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail={
            "code": "bad_status", "value": status})
    _ = user
    brand_id, group_id = scope_filters(user)
    async with pool.acquire() as conn:
        row = await _get_connector_row(conn, connector_id)
        rows = await conn.fetch(Q["connector_sync_runs"], row["id"],
                                brand_id, group_id, limit, entity, status)
        items = [_run_row(r) for r in rows]
    return {"connector_id": str(row["id"]), "sync_runs": items}
