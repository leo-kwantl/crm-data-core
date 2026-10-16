"""Connector sync-worker heartbeat (LD2) — data-owned, worker layer.

`note_sync_result` closes a `connector_sync_runs` row AND stamps the
connector's `last_sync_at`/`last_sync_status` checkpoint in one
transaction: the *write* half of the LD2 checkpoint (`025_connector_schedule.sql`).

Why it sits here rather than in `services/api/repos/connectors.py`
(moved 2026-10-17): the checkpoint is **worker-owned**. App's serving
layer only reads it — `GET /connectors/{id}` renders
`last_sync_at`/`last_sync_status`, and `PUT /connectors/{id}/schedule`
touches `schedule_cron` alone (app's docstring on that route: "the sync
checkpoint is worker-owned and is read-only here"). Data's LD2 delivery
(`4fa0cc3`) had put the writer inside `services/api/repos/connectors.py`,
i.e. inside a file whose base is app's, and app's later connector commit
dropped it there for exactly that reason. So the writer lives in a
pipeline module: no serving-layer import, no fork of an app-owned file.

Lifecycle split (019 contract): the API *enqueues* the run
(`POST /connectors/{id}/sync` → `queued` row); everything after that is
the worker's, including this result + checkpoint write.

Caller contract (pipelines/contracts.md §019 LD2): `status` one of
`queued|running|succeeded|failed`; counts >= 0 (`ValueError` otherwise);
`LookupError` for an unknown run; `started_at` filled on first touch and
`finished_at` only on a terminal status. The caller — the sync worker —
owns retries; one transaction means a failed checkpoint write cannot
leave a closed run row behind.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

SYNC_STATUSES = ("queued", "running", "succeeded", "failed")
TERMINAL_STATUSES = ("succeeded", "failed")


async def note_sync_result(pool: asyncpg.Pool, run_id: UUID, status: str,
                           rows_seen: int = 0, rows_ok: int = 0,
                           rows_rejected: int = 0,
                           error: str | None = None) -> dict[str, Any]:
    """Close run `run_id` and stamp the connector checkpoint (one txn).

    Returns `{"run_id": <str>, "status": <str>}` — the caller logs it and
    the connector's health, not this function.
    """
    if status not in SYNC_STATUSES:
        raise ValueError(f"bad sync status: {status!r}")
    for label, n in (("rows_seen", rows_seen), ("rows_ok", rows_ok),
                     ("rows_rejected", rows_rejected)):
        if not isinstance(n, int) or n < 0:
            raise ValueError(f"{label} must be >= 0, got {n!r}")
    async with pool.acquire() as conn:
        async with conn.transaction():
            run = await conn.fetchrow(
                "UPDATE connector_sync_runs"
                " SET status = $2, rows_seen = $3, rows_ok = $4,"
                " rows_rejected = $5, error = $6,"
                " started_at = COALESCE(started_at, now()),"
                " finished_at = CASE WHEN $2 IN"
                " ('succeeded', 'failed') THEN now() ELSE finished_at END"
                " WHERE id = $1"
                " RETURNING id, connector_id",
                run_id, status, rows_seen, rows_ok, rows_rejected, error)
            if run is None:
                raise LookupError(f"sync run {run_id} not found")
            await conn.execute(
                "UPDATE connectors SET last_sync_at = now(),"
                " last_sync_status = $2, updated_at = now()"
                " WHERE id = $1",
                run["connector_id"], status)
    return {"run_id": str(run["id"]), "status": status}
