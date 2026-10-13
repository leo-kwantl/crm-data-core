"""Consent repository: pure SQL over asyncpg. Single source of truth is
db/queries/consents.sql (parsed by -- name: markers).

WRITE RULE: replace_consents() writes `consents` (upsert) AND appends to
`consent_events` inside ONE transaction. `consent_events` rows are only ever
INSERTed/SELECTed here — never UPDATED or DELETED (migration also REVOKEs
UPDATE/DELETE on the table from PUBLIC).

Profile-derivation stub for reuse (profile agent): derive_contactability()
pure-python helper + customer_subscription_derived SQL query above.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

QUERIES_PATH = Path(__file__).resolve().parents[3] / "db" / "queries" / "consents.sql"

_NAME_RE = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.M)


def load_queries() -> dict[str, str]:
    text = QUERIES_PATH.read_text(encoding="utf-8")
    parts = _NAME_RE.split(text)
    # parts: [preamble, name1, sql1, name2, sql2, ...]
    out: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        name, sql = parts[i], parts[i + 1]
        # strip trailing line-comments that start another section (view comment block)
        lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("-- name:")]
        out[name] = "\n".join(lines).strip().rstrip(";")
    return out


def derive_contactability(rows: Iterable[Mapping[str, Any]]) -> bool:
    """Overall subscription status: contactable iff ANY channel is opted_in.

    Reusable by the profile endpoint (profile surfaces not-contactable flag).
    Customers with no consent rows are treated as NOT contactable (default-deny).
    """
    return any(r.get("status") == "opted_in" for r in rows)


async def customer_exists(conn, customer_id: UUID) -> bool:
    q = load_queries()
    row = await conn.fetchrow(q["customer_exists"], customer_id)
    return row is not None


async def get_consents(conn, customer_id: UUID) -> list[dict]:
    q = load_queries()
    rows = await conn.fetch(q["consents_by_customer"], customer_id)
    return [dict(r) for r in rows]


async def get_consent_events(conn, customer_id: UUID) -> list[dict]:
    q = load_queries()
    rows = await conn.fetch(q["consent_events_by_customer"], customer_id)
    return [dict(r) for r in rows]


async def get_subscription_derived(conn, customer_id: UUID) -> dict:
    q = load_queries()
    row = await conn.fetchrow(q["customer_subscription_derived"], customer_id)
    return dict(row) if row else {
        "is_contactable_for_marketing": False,
        "opted_in_channels": 0,
        "channel_count": 0,
    }


async def replace_consents(
    pool,
    customer_id: UUID,
    items: list[dict],
    default_captured_by: str,
    brand_id=None,
) -> list[dict]:
    """Upsert full channel-status statement + append one event per channel,
    atomically in a single transaction. Returns the new current state rows."""
    q = load_queries()
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchrow(q["customer_exists"], customer_id)
            if exists is None:
                raise KeyError(str(customer_id))
            for item in items:
                captured_by = item.get("captured_by") or default_captured_by
                await conn.fetchrow(
                    q["consent_upsert"],
                    customer_id,
                    item["channel"],
                    item["status"],
                    captured_by,
                )
                await conn.fetchrow(
                    q["consent_event_insert"],
                    customer_id,
                    item["channel"],
                    item["status"],
                    captured_by,
                )
            if brand_id is not None:
                # Scope new writes per brand: stamp untouched rows only, so
                # legacy NULL-brand history keeps its pre-brand meaning.
                await conn.execute(
                    "UPDATE consents SET brand_id = $2"
                    " WHERE customer_id = $1 AND brand_id IS NULL",
                    customer_id,
                    brand_id,
                )
            rows = await conn.fetch(q["consents_by_customer"], customer_id)
            return [dict(r) for r in rows]
