"""Backfill derived order_placed events for pre-existing orders (INSERT-only).

One customer_events row per order: kind='order_placed', source='derived',
source_event_id='order:'||order_id, occurred_at=ordered_at. Skips orders
that already have the derived event, so reruns are no-ops. Applies to ANY
database (dev seed, prod backfill) — pass --database-url explicitly.

ML calibration excludes kind='order_placed' by name (see contracts.md kind
vocabulary); never rename without ml ack.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import asyncpg

from pipelines.apply import (DERIVED_EVENT_SOURCE, DERIVED_ORDER_KIND,
                             FIND_EVENT_SQL, INSERT_EVENT_SQL)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    if not args.database_url:
        print("set --database-url", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(args.database_url)
    try:
        q = ("SELECT o.id, o.customer_id, o.store_id, o.ordered_at,"
             " o.total_cents, o.external_order_id FROM orders o"
             " WHERE NOT EXISTS (SELECT 1 FROM customer_events ce WHERE"
             " ce.source=$1 AND ce.source_event_id='order:'||o.id::text)"
             " ORDER BY o.ordered_at")
        if args.limit:
            q += f" LIMIT {int(args.limit)}"
        rows = await conn.fetch(q, DERIVED_EVENT_SOURCE)
        n = 0
        for r in rows:
            oid = str(r["id"])
            label = r["external_order_id"] or oid[:8]
            summary = f"Order {label} \u00b7 ${r['total_cents'] / 100:,.2f}"[:200]
            payload = json.dumps({"order_id": oid,
                                  "total_cents": r["total_cents"],
                                  "store_id": str(r["store_id"])})
            await conn.execute(
                INSERT_EVENT_SQL, str(r["customer_id"]), DERIVED_ORDER_KIND,
                DERIVED_EVENT_SOURCE, f"order:{oid}", summary, payload,
                r["ordered_at"])
            n += 1
        print(f"order_placed events inserted: {n} (orders missing derived: {len(rows)})")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
