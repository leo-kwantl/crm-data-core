"""The dashboard's rollup read: KPI daily snapshots served from the shared file.

The dashboard and comparison pages read `kpi_daily_snapshots` instead of
counting the live tables: this module parses `db/queries/kpi_daily.sql`
by its `-- name:` markers (the same convention as
`analytics/snapshots_serve.py`), so the file IS the text and no inline
copy can drift from it.

Scope is the row's own stamp compared scope-exactly (the file's own
rule): the session's (brand_id, group_id) IS NOT DISTINCT FROM the
row's, so a brand session reads its own row and never the global one.
A scope with no snapshot rows reads an empty curve, not a 404: the
rollup has one row per day x scope and coverage is sparse (the nightly
writes it; a backfill wrote the three global rows), so an empty read is
"nothing written yet" and the page falls back to its live series.

Vintage: `snapshot_date` is the business day the row describes;
`created_at` is when the nightly wrote it. One vintage per curve
(max(created_at) over the rows read) travels with the read.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import asyncpg

QUERIES_PATH = (Path(__file__).resolve().parents[3] / "db" / "queries"
                / "kpi_daily.sql")

_NAME_RE = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.M)

# How far back the dashboard's daily curve reaches from its as-of day.
# The table holds one row per day x scope; 90 daily rows is one round trip.
ROLLUP_WINDOW_DAYS = 90


def load_queries() -> dict[str, str]:
    """The shared file's named statements, by name (parsed, never copied)."""
    text = QUERIES_PATH.read_text(encoding="utf-8")
    parts = _NAME_RE.split(text)
    out: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        out[parts[i]] = parts[i + 1].strip().rstrip(";")
    return out


Q = load_queries()


def _iso(v: Any) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else v


async def get_rollup(conn: asyncpg.Connection, *, as_of: date,
                     brand_id: Any = None,
                     group_id: Any = None) -> dict[str, Any]:
    """The scope's daily curve ending on `as_of`, plus its day-over-day move.

    `points` are oldest-first; `vintage` is max(created_at) over them.
    `pct_change` is None when either leg of the pair is missing (the JOIN
    needs the day before `as_of` in the same scope). A scope with no rows
    reads `points: []`, `vintage: None`, `pct_change: None`.
    """
    date_to = as_of
    date_from = as_of - timedelta(days=ROLLUP_WINDOW_DAYS - 1)
    rows = await conn.fetch(Q["kpi_trend"], date_from, date_to,
                            brand_id, group_id)
    pct = await conn.fetchrow(Q["kpi_pct_change"], as_of,
                              brand_id, group_id)
    points = [
        {
            "date": _iso(r["snapshot_date"]),
            "customers_total": int(r["customers_total"]),
            "orders_total": int(r["orders_total"]),
            "revenue_cents_total": int(r["revenue_cents_total"]),
            "orders_day": int(r["orders_day"]),
            "revenue_day_cents": int(r["revenue_day_cents"]),
            "new_customers_day": int(r["new_customers_day"]),
            "active_30d": int(r["active_30d"]),
        }
        for r in rows
    ]
    created = [r["created_at"] for r in rows
               if r["created_at"] is not None]
    return {
        "vintage": _iso(max(created)) if created else None,
        "date_from": _iso(date_from),
        "date_to": _iso(date_to),
        "points": points,
        "pct_change": (
            None if pct is None else {
                "as_of": _iso(pct["as_of"]),
                "revenue_day_pct": (float(pct["revenue_day_pct"])
                                    if pct["revenue_day_pct"] is not None
                                    else None),
                "orders_day_pct": (float(pct["orders_day_pct"])
                                   if pct["orders_day_pct"] is not None
                                   else None),
                "active_30d_pct": (float(pct["active_30d_pct"])
                                   if pct["active_30d_pct"] is not None
                                   else None),
            }
        ),
    }
