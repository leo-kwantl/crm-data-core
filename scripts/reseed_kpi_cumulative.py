"""Reseed the KPI ledger's cumulative columns (data owns the history).

WHY: the cumulative columns of a `kpi_daily_snapshots` row used to count
the WHOLE present table, so a row written on a past day carried the
present's numbers under that day's label (the manager measured it on the
live cluster: the row labelled 2026-10-04 carried `orders_total=8689`
while the present was 8686). The writer now bounds the cumulative
columns by the row's own day (`pipelines/snapshot.py`,
`KPI_DAILY_UPSERT_SQL`), so every row that was written under the old
rule carries numbers that no rule can explain.

THE ORDER (the two steps are not interchangeable):

1. The FIX ships first. A reseed run against the old writer recomputes
   the old numbers -- a no-op that looks like a success.
2. Then the reseed walks the rows in ASCENDING snapshot_date and
   rewrites each one: the row is deleted and written again through the
   writer's own statement, so the reseeded numbers come from the SAME
   SQL that writes new rows (one home for the rule -- a second copy in
   this script would be free to drift).
3. Then ANALYZE, because every row changed and the planner's numbers
   are stale otherwise.

The values are order-independent (each row is bounded by its own day),
so the order only decides WHICH rows are corrected if the run is
interrupted: ascending leaves the oldest rows corrected and the newest
carrying the old rule, and every row is corrected on the next run.

    DATABASE_URL=postgresql://... python scripts/reseed_kpi_cumulative.py

Refuses a default DSN: a target that is not printed is a target nobody
can check, and a DELETE aimed at the wrong cluster is the one mistake a
second run cannot fix.
"""
from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipelines.snapshot import KPI_DAILY_UPSERT_SQL  # noqa: E402


def resolve_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit(
            "[REFUSED] DATABASE_URL is unset -- this script never guesses a "
            "cluster. Set it to the target and read the printed "
            "host:port/db before it writes.")
    return dsn


def describe(dsn: str) -> str:
    """The target as host:port/db -- what the process will actually use."""
    tail = dsn.split("://", 1)[-1]
    tail = tail.split("@")[-1].split("/")[0]
    host, _, port = tail.partition(":")
    db = dsn.rsplit("/", 1)[-1].split("?")[0]
    return f"{host}:{port or '5432'}/{db}"


async def reseed(dsn: str) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT id, snapshot_date, brand_id, group_id"
            " FROM kpi_daily_snapshots ORDER BY snapshot_date ASC, id ASC")
        print(f"[SEED ] {len(rows)} rows to reseed (ascending snapshot_date)")
        changed = 0
        for row in rows:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM kpi_daily_snapshots WHERE id = $1", row["id"])
                await conn.execute(
                    KPI_DAILY_UPSERT_SQL, row["snapshot_date"],
                    row["brand_id"], row["group_id"])
            changed += 1
            print(f"[SEED ] {row['snapshot_date']} brand={row['brand_id']} "
                  f"group={row['group_id']} reseeded ({changed}/{len(rows)})")
        await conn.execute("ANALYZE kpi_daily_snapshots")
        print("[SEED ] ANALYZE kpi_daily_snapshots done")
        return 0
    finally:
        await conn.close()


def main() -> int:
    dsn = resolve_dsn()
    print(f"[START] the KPI ledger's cumulative columns | target {describe(dsn)}")
    return asyncio.run(reseed(dsn))


if __name__ == "__main__":
    raise SystemExit(main())
