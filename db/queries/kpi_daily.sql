-- KPI daily snapshots: the serving reads of the daily rollup.
--
-- SHARED FILE (2026-10-22): app mirrors this file byte-for-byte and
-- serves it by its `-- name:` markers, so every statement here is in the
-- SERVING form -- asyncpg $n placeholders, no named-parameter dialect.
--
-- SCOPE: the row's own stamp, compared scope-exactly -- the rule of
-- ../docs/conventions.md § Scope shape and the same one
-- `pipelines/snapshot.py::KPI_TREND_SQL` uses: the session's
-- (brand_id, group_id) IS NOT DISTINCT FROM the row's, so a brand session
-- reads its own row and never the global one, and no session reads a
-- group-stamped row (the group scope was withdrawn). The scope parameters
-- are always the LAST two.
--
-- VINTAGE: `snapshot_date` is the business day the row describes;
-- `created_at` is when the nightly run wrote it. A reader shows ONE
-- vintage for a curve -- max(created_at) over the rows it read -- so the
-- column travels with the read.
--
-- The WRITER of these rows is the pipeline's, not a serving statement:
-- `pipelines/snapshot.py::KPI_DAILY_UPSERT_SQL` (one row per day x scope,
-- idempotent).

-- name: kpi_trend
-- params: date_from date, date_to date, brand_id uuid|None, group_id uuid|None
-- The daily rollup for one scope, oldest first: the cumulative columns
-- (customers_total, orders_total, revenue_cents_total) are the state as
-- of that row's own day, and the day columns (orders_day, revenue_day_cents,
-- new_customers_day, active_30d) are that day's movement.
SELECT snapshot_date, customers_total, orders_total, revenue_cents_total,
       orders_day, revenue_day_cents, new_customers_day, active_30d,
       created_at
FROM kpi_daily_snapshots
WHERE snapshot_date >= $1::date AND snapshot_date <= $2::date
  AND brand_id IS NOT DISTINCT FROM $3::uuid
  AND group_id IS NOT DISTINCT FROM $4::uuid
ORDER BY snapshot_date;

-- name: kpi_pct_change
-- params: as_of date, brand_id uuid|None, group_id uuid|None
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
WHERE cur.snapshot_date = $1::date
  AND cur.brand_id IS NOT DISTINCT FROM $2::uuid
  AND cur.group_id IS NOT DISTINCT FROM $3::uuid;

-- name: clv_snapshots_list
-- params: limit int, brand_id uuid|None, group_id uuid|None
SELECT id, as_of, horizon_days, customer_count, created_by, created_at,
       brand_id, group_id
FROM clv_snapshots
WHERE brand_id IS NOT DISTINCT FROM $2::uuid
  AND group_id IS NOT DISTINCT FROM $3::uuid
ORDER BY as_of DESC LIMIT $1::int;

-- name: churn_snapshots_list
-- params: limit int, brand_id uuid|None, group_id uuid|None
SELECT id, as_of, activity, horizon_days, customer_count, created_by,
       created_at, brand_id, group_id
FROM churn_snapshots
WHERE brand_id IS NOT DISTINCT FROM $2::uuid
  AND group_id IS NOT DISTINCT FROM $3::uuid
ORDER BY as_of DESC LIMIT $1::int;
