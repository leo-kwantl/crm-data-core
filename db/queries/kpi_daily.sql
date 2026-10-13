-- KPI daily snapshots + history helpers (mirrors pipelines/snapshot.py).
-- One row per day x scope; trend/pct-change reads filter by scope.

-- name: upsert_kpi_daily
-- Writes today's (or :as_of) grain row. Idempotent per day x scope.
-- (Full statement lives in pipelines/snapshot.py:KPI_DAILY_UPSERT_SQL.)

-- name: kpi_trend
SELECT snapshot_date, customers_total, orders_total, revenue_cents_total,
       orders_day, revenue_day_cents, new_customers_day, active_30d
FROM kpi_daily_snapshots
WHERE snapshot_date >= :date_from AND snapshot_date <= :date_to
  AND brand_id IS NOT DISTINCT FROM :brand_id
  AND group_id IS NOT DISTINCT FROM :group_id
ORDER BY snapshot_date;

-- name: kpi_pct_change
-- Two-day pct change for headline metrics (NULL when prev = 0).
SELECT cur.snapshot_date AS as_of,
  CASE WHEN prev.revenue_day_cents = 0 THEN NULL
       ELSE (cur.revenue_day_cents - prev.revenue_day_cents)::float
            / prev.revenue_day_cents END AS revenue_day_pct,
  CASE WHEN prev.orders_day = 0 THEN NULL
       ELSE (cur.orders_day - prev.orders_day)::float
            / prev.orders_day END AS orders_day_pct,
  CASE WHEN prev.active_30d = 0 THEN NULL
       ELSE (cur.active_30d - prev.active_30d)::float
            / prev.active_30d END AS active_30d_pct
FROM kpi_daily_snapshots cur
JOIN kpi_daily_snapshots prev
  ON prev.snapshot_date = cur.snapshot_date - 1
 AND prev.brand_id IS NOT DISTINCT FROM cur.brand_id
 AND prev.group_id IS NOT DISTINCT FROM cur.group_id
WHERE cur.snapshot_date = :as_of
  AND cur.brand_id IS NOT DISTINCT FROM :brand_id
  AND cur.group_id IS NOT DISTINCT FROM :group_id;

-- name: clv_snapshots_list
SELECT id, as_of, horizon_days, customer_count, created_by, created_at,
       brand_id, group_id
FROM clv_snapshots
WHERE brand_id IS NOT DISTINCT FROM :brand_id
  AND group_id IS NOT DISTINCT FROM :group_id
ORDER BY as_of DESC LIMIT :limit;

-- name: churn_snapshots_list
SELECT id, as_of, activity, horizon_days, customer_count, created_by,
       created_at, brand_id, group_id
FROM churn_snapshots
WHERE brand_id IS NOT DISTINCT FROM :brand_id
  AND group_id IS NOT DISTINCT FROM :group_id
ORDER BY as_of DESC LIMIT :limit;
