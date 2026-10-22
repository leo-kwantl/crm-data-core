"""Raw-SQL repo for customer journey events. Pure SQL via asyncpg, no ORM.

Append-only: rows are only ever INSERTed/SELECTed here — never UPDATED or
DELETED (migration also REVOKEs UPDATE/DELETE on the table from PUBLIC).
Customer deletion cascades (FK ON DELETE CASCADE); no destructive test —
cascade is verified read-only via the pg catalog in tests.

Cursor = base64url JSON {"at": <occurred_at iso>, "id": <uuid>} keying
(occurred_at DESC, id DESC) pagination, newest-first.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime

import asyncpg

CUSTOMER_EXISTS_SQL = """
-- name: customer_exists
-- params: customer_id uuid
SELECT 1 FROM customers WHERE id = $1 AND deleted_at IS NULL;
"""

CUSTOMER_HEADER_SQL = (
    "SELECT member_id, full_name FROM customers WHERE id = $1 AND deleted_at IS NULL"
)

INSERT_EVENT_SQL = """
INSERT INTO customer_events (customer_id, occurred_at, kind, source, summary, payload, created_by)
VALUES ($1, COALESCE($2::timestamptz, now()), $3, $4, $5, $6::jsonb, $7)
RETURNING id, customer_id, occurred_at, kind, source, summary, payload, created_by, created_at
"""

LIST_EVENTS_SQL = """
SELECT id, customer_id, occurred_at, kind, source, summary, payload, created_by, created_at
FROM customer_events
WHERE customer_id = $1
  AND ($2::timestamptz IS NULL OR (occurred_at, id) < ($2::timestamptz, $3::uuid))
ORDER BY occurred_at DESC, id DESC
LIMIT $4
"""


def encode_cursor(at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps({"at": at.isoformat(), "id": str(row_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Return (occurred_at, id). Raises ValueError on malformed cursors."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(payload["at"]), uuid.UUID(payload["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_cursor") from exc


async def customer_exists(conn: asyncpg.Connection, customer_id: uuid.UUID) -> bool:
    return await conn.fetchval(CUSTOMER_EXISTS_SQL, customer_id) is not None


async def get_customer_header(
    conn: asyncpg.Connection, customer_id: uuid.UUID
) -> dict | None:
    row = await conn.fetchrow(CUSTOMER_HEADER_SQL, customer_id)
    return dict(row) if row else None


async def insert_event(
    conn: asyncpg.Connection,
    customer_id: uuid.UUID,
    *,
    kind: str,
    source: str,
    summary: str,
    payload: dict,
    created_by: str | None,
) -> dict:
    row = await conn.fetchrow(
        INSERT_EVENT_SQL,
        customer_id,
        None,
        kind,
        source,
        summary,
        json.dumps(payload),
        created_by,
    )
    assert row is not None
    result = dict(row)
    if isinstance(result.get("payload"), str):
        result["payload"] = json.loads(result["payload"])
    return result


async def list_customer_events(
    conn: asyncpg.Connection,
    customer_id: uuid.UUID,
    *,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict], str | None]:
    """Newest-first page. Returns (rows, next_cursor); caller fetches limit+1."""
    cursor_at: datetime | None = None
    cursor_id: uuid.UUID | None = None
    if cursor:
        cursor_at, cursor_id = decode_cursor(cursor)
    rows = await conn.fetch(LIST_EVENTS_SQL, customer_id, cursor_at, cursor_id, limit + 1)
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("payload"), str):
            d["payload"] = json.loads(d["payload"])
        result.append(d)
    next_cursor = None
    if len(result) > limit:
        result.pop()  # overflow row, only proves a next page exists
        last = result[-1]
        next_cursor = encode_cursor(last["occurred_at"], last["id"])
    return result, next_cursor


# --- Numbered (offset) page: same newest-first order, additive ---

COUNT_EVENTS_SQL = """
SELECT COUNT(*) FROM customer_events WHERE customer_id = $1
"""

OFFSET_EVENTS_SQL = """
SELECT id, customer_id, occurred_at, kind, source, summary, payload, created_by, created_at
FROM customer_events
WHERE customer_id = $1
ORDER BY occurred_at DESC, id DESC
LIMIT $2 OFFSET $3
"""


async def list_customer_events_offset(
    conn: asyncpg.Connection,
    customer_id: uuid.UUID,
    *,
    page: int,
    page_size: int,
) -> dict:
    """Numbered page (1-based) of one customer's timeline."""
    from .paging import offset_payload

    total = await conn.fetchval(COUNT_EVENTS_SQL, customer_id)
    rows = await conn.fetch(
        OFFSET_EVENTS_SQL, customer_id, page_size, (page - 1) * page_size
    )
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("payload"), str):
            d["payload"] = json.loads(d["payload"])
        result.append(d)
    return offset_payload(result, int(total or 0), page, page_size)
