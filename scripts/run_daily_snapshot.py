"""Daily snapshot job (data owns history; app serves, ML scores).

Writes one kpi_daily_snapshots row for --as-of (default: today), idempotent
per day x scope (re-run = no-op via ON CONFLICT DO NOTHING). Run daily via
cron/scheduler, once per scope that needs trends:

  python scripts/run_daily_snapshot.py --as-of 2026-10-09
  python scripts/run_daily_snapshot.py --as-of 2026-10-09 --brand-id UUID --group-id UUID

After the KPI row, persist model runs with the sibling ML jobs (history
tables live here in 013, compute lives there):
  python -m ml_jobs.run_snapshot "$DATABASE_URL" --mode rolling_365 --as-of <date>
  python -m ml_jobs.run_recs_batch "$DATABASE_URL" ...
Then record the run in model_training_runs (params/metrics/snapshot_id).

DEV/CI: uses services.api.db.get_database_url (pgembed when DATABASE_URL
absent). NEVER point at a live DB for bootstrap; this job is INSERT-only
and safe to run against live (it never truncates).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipelines.snapshot import KPI_DAILY_UPSERT_SQL  # noqa: E402
from services.api.db import get_database_url  # noqa: E402


def _uuid_or_none(v: str | None) -> uuid.UUID | None:
    return uuid.UUID(v) if v else None


async def run(as_of: date, brand_id, group_id) -> dict:
    import asyncpg

    conn = await asyncpg.connect(get_database_url())
    try:
        row = await conn.fetchrow(KPI_DAILY_UPSERT_SQL, as_of, brand_id, group_id)
        if row is None:  # conflict no-op: re-run of the same day x scope
            row = await conn.fetchrow(
                "SELECT id, snapshot_date, customers_total, orders_total,"
                " revenue_cents_total, orders_day, revenue_day_cents,"
                " new_customers_day, active_30d FROM kpi_daily_snapshots"
                " WHERE snapshot_date = $1::date"
                " AND brand_id IS NOT DISTINCT FROM $2::uuid"
                " AND group_id IS NOT DISTINCT FROM $3::uuid",
                as_of, brand_id, group_id)
        assert row is not None
        return dict(row)
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Write one KPI daily snapshot row")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--brand-id", default=None)
    ap.add_argument("--group-id", default=None)
    args = ap.parse_args()
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    out = asyncio.run(run(as_of, _uuid_or_none(args.brand_id),
                          _uuid_or_none(args.group_id)))
    print(out)


if __name__ == "__main__":
    main()
