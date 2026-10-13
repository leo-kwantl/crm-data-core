"""Daily snapshot helpers: KPI grain + pct-change / movement math.

Downstream contract (app serves, ML scores, DATA persists history):
- RFM already snapshots (rfm_snapshots); recs batch (rec_batches).
- CLV/churn/segments are live-compute only — 013_analytics_history.sql
  adds clv/churn snapshot tables + kpi_daily_snapshots + training runs.
- This module is the data-side runner math: pure functions (unit-tested
  without a DB) + the parameterised SQL the daily job uses.

Daily job: scripts/run_daily_snapshot.py --as-of YYYY-MM-DD
  (idempotent per day x scope via the COALESCE UNIQUE indexes).
"""
from __future__ import annotations


def pct_change(prev: float, cur: float) -> float | None:
    """Relative change (cur-prev)/prev. None when prev == 0 (undefined)."""
    if prev == 0:
        return None
    return (cur - prev) / prev


def kpi_movement(prev: dict, cur: dict) -> dict:
    """Compare two kpi_daily_snapshots rows -> deltas + pct per metric."""
    metrics = ("customers_total", "orders_total", "revenue_cents_total",
               "orders_day", "revenue_day_cents", "new_customers_day",
               "active_30d")
    deltas = {}
    for m in metrics:
        p, c = float(prev.get(m, 0)), float(cur.get(m, 0))
        deltas[m] = {"prev": p, "cur": c, "delta": c - p,
                     "pct": pct_change(p, c)}
    return {"from": prev.get("snapshot_date"), "to": cur.get("snapshot_date"),
            "deltas": deltas}


def clv_movement(prev_scores: dict, cur_scores: dict) -> dict:
    """Compare two CLV runs keyed by customer_id -> entered/exited/moved.

    Each score: {clv_cents, value_tier, p_alive}. Moved = tier changed or
    |clv| moved >= 10% (tunable threshold, kept explicit for tests).
    """
    prev_ids, cur_ids = set(prev_scores), set(cur_scores)
    both = prev_ids & cur_ids
    moved = []
    for cid in sorted(both):
        p, c = prev_scores[cid], cur_scores[cid]
        pc = pct_change(float(p.get("clv_cents", 0)), float(c.get("clv_cents", 0)))
        tier_flip = p.get("value_tier") != c.get("value_tier")
        if tier_flip or (pc is not None and abs(pc) >= 0.10):
            moved.append({"customer_id": cid,
                          "clv_prev": p.get("clv_cents"),
                          "clv_cur": c.get("clv_cents"),
                          "pct": pc, "tier_prev": p.get("value_tier"),
                          "tier_cur": c.get("value_tier")})
    return {"customers_prev": len(prev_ids), "customers_cur": len(cur_ids),
            "entered": sorted(cur_ids - prev_ids),
            "exited": sorted(prev_ids - cur_ids),
            "moved": moved}


# --- KPI daily-grain SQL (one row per day x scope, idempotent re-run) ---

KPI_DAILY_UPSERT_SQL = """
WITH day_bounds AS (
  SELECT ($1::date)::timestamptz AS d_from,
         (($1::date + 1))::timestamptz AS d_to,
         (($1::date - 30))::timestamptz AS act_from
),
scoped_orders AS (
  SELECT o.id, o.total_cents, o.ordered_at, o.customer_id
  FROM orders o
  JOIN stores s ON s.id = o.store_id
  JOIN brands b ON b.id = s.brand_id
  WHERE o.status = 'completed'
    AND ($2::uuid IS NULL OR s.brand_id = $2::uuid)
    AND ($3::uuid IS NULL OR b.group_id = $3::uuid)
),
scoped_customers AS (
  SELECT c.id, c.created_at
  FROM customers c
  WHERE c.deleted_at IS NULL
    AND ($3::uuid IS NULL OR c.group_id = $3::uuid)
    AND ($2::uuid IS NULL OR EXISTS (
      SELECT 1 FROM orders o2 JOIN stores s2 ON s2.id = o2.store_id
      WHERE o2.customer_id = c.id AND o2.status = 'completed'
        AND s2.brand_id = $2::uuid))
)
INSERT INTO kpi_daily_snapshots
  (snapshot_date, customers_total, orders_total, revenue_cents_total,
   orders_day, revenue_day_cents, new_customers_day, active_30d,
   brand_id, group_id)
SELECT $1::date,
  (SELECT COUNT(*) FROM scoped_customers),
  (SELECT COUNT(*) FROM scoped_orders),
  (SELECT COALESCE(SUM(total_cents), 0) FROM scoped_orders),
  (SELECT COUNT(*) FROM scoped_orders, day_bounds
    WHERE ordered_at >= d_from AND ordered_at < d_to),
  (SELECT COALESCE(SUM(total_cents), 0) FROM scoped_orders, day_bounds
    WHERE ordered_at >= d_from AND ordered_at < d_to),
  (SELECT COUNT(*) FROM scoped_customers, day_bounds
    WHERE created_at >= d_from AND created_at < d_to),
  (SELECT COUNT(DISTINCT customer_id) FROM scoped_orders, day_bounds
    WHERE ordered_at >= act_from AND ordered_at < d_to),
  $2::uuid, $3::uuid
ON CONFLICT DO NOTHING
RETURNING id, snapshot_date, customers_total, orders_total,
  revenue_cents_total,
  orders_day, revenue_day_cents, new_customers_day, active_30d
"""

KPI_TREND_SQL = """
SELECT snapshot_date, customers_total, orders_total, revenue_cents_total,
       orders_day, revenue_day_cents, new_customers_day, active_30d
FROM kpi_daily_snapshots
WHERE snapshot_date >= $1::date AND snapshot_date <= $2::date
  AND brand_id IS NOT DISTINCT FROM $3::uuid
  AND group_id IS NOT DISTINCT FROM $4::uuid
ORDER BY snapshot_date
"""
