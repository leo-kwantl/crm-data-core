-- Named queries for period-over-period comparisons (MoM / YoY).
-- Convention: `-- name: <query_name>` + `-- params: ...` comment lines
-- (parsed by services/api/analytics/periods.py, same marker convention as
-- db/queries/analytics_snapshots.sql). READ-ONLY: SELECTs only.
--
-- WINDOW SEMANTICS -- calendar month-to-date, always like-for-like:
--   current : 1st of the reference month  -> reference day (inclusive)
--   MoM     : same day range of the previous month
--   YoY     : same day range of the same month one year earlier
--   YTD     : 1st of the reference year -> reference day (inclusive).
--              a plain total, never a percentage: the year is still in
--              progress, so there is no like-for-like baseline to compare.
-- Every window is cut at the SAME day-of-month. Comparing a partial
-- month-to-date against a FULL previous month would fabricate growth
-- (a 9-day month would always "fall"), so the previous-month window is
-- clipped to the same number of days and clamped to its own month end.
--
-- The reference date is resolved from the DATA (max order date), never
-- from now(), so a frozen seed keeps reporting the month it actually
-- contains. Callers may override it by passing a date.
--
-- SCOPE ($brand / $group, always the LAST two params): serving-side row
-- visibility, owned by services/api/analytics/scope.py. Each filter is
-- applied only when non-NULL, so brand-scoped sessions see their brand's
-- stores, group-wide sessions see their group's stores, and HQ (NULL/NULL)
-- sees everything. Customers carry group_id but no brand, so the brand
-- filter cannot narrow customer-derived rows -- documented per query.
--
-- Availability is deliberately NOT decided here: the aggregate queries
-- return raw SUM/COUNT (SUM is NULL for an empty window) and the caller
-- compares the window against window_bounds.data_min / data_max to decide
-- whether a baseline is "missing" rather than a real zero. This is what
-- lets the UI print "n/a - no data before 2025-10-10" instead of a
-- fabricated -100%.

-- name: window_bounds
-- params: ref_date date|None, brand uuid|None, group uuid|None
-- Resolves the comparison windows — current, MoM, YoY, YTD (half-open
-- [start, end)) — from a reference date, defaulting to the max order date IN
-- SCOPE. Also returns the scoped data coverage so the caller can explain a
WITH scoped AS (
    SELECT o.ordered_at
    FROM orders o
    JOIN stores s ON s.id = o.store_id
    LEFT JOIN brands b ON b.id = s.brand_id
    LEFT JOIN groups g ON g.id = b.group_id
    WHERE ($2::uuid IS NULL OR b.id = $2::uuid)
      AND ($3::uuid IS NULL OR g.id = $3::uuid)
), base AS (
    SELECT COALESCE($1::date, (SELECT MAX(ordered_at)::date FROM scoped)) AS ref_date
), b AS (
    SELECT ref_date,
           date_trunc('month', ref_date)::date AS month_start,
           (ref_date - date_trunc('month', ref_date)::date) AS day_offset
    FROM base
)
SELECT b.ref_date,
       b.month_start AS cur_start,
       (b.ref_date + 1) AS cur_end,
       (b.month_start - interval '1 month')::date AS mom_start,
       LEAST((b.month_start - interval '1 month')::date + b.day_offset + 1,
             b.month_start) AS mom_end,
       (b.month_start - interval '1 year')::date AS yoy_start,
       LEAST((b.month_start - interval '1 year')::date + b.day_offset + 1,
             (b.month_start - interval '1 year' + interval '1 month')::date) AS yoy_end,
       date_trunc('year', b.ref_date)::date AS ytd_start,
       (b.ref_date + 1) AS ytd_end,
       (SELECT MIN(ordered_at)::date FROM scoped) AS data_min,
       (SELECT MAX(ordered_at)::date FROM scoped) AS data_max
FROM b;

-- name: overview_comparison
-- params: cur_start date, cur_end date, mom_start date, mom_end date, yoy_start date, yoy_end date, ytd_start date, ytd_end date, brand uuid|None, group uuid|None
-- NOTE: the ::date casts on $1..$8 are REQUIRED, not cosmetic. Compared
-- against a timestamptz column without a cast, PostgreSQL infers the
-- parameter as timestamptz and the driver encodes a Python date as local
-- midnight, shifting every window by the session UTC offset (UTC+8 here),
-- which silently pulled the previous month's last day into MoM/YoY.
-- The four headline figures of the dashboard, each over the MoM/YoY windows
-- plus the year-to-date total (1 Jan -> reference day), in ONE round-trip.
-- revenue_* is NULL when the window has no orders.
-- New-customer counts are group-scoped only (customers carry no brand).
WITH scoped AS (
    SELECT o.ordered_at, o.total_cents, o.customer_id
    FROM orders o
    JOIN stores s ON s.id = o.store_id
    LEFT JOIN brands b ON b.id = s.brand_id
    LEFT JOIN groups g ON g.id = b.group_id
    WHERE ($9::uuid IS NULL OR b.id = $9::uuid)
      AND ($10::uuid IS NULL OR g.id = $10::uuid)
), scoped_cust AS (
    SELECT c.created_at
    FROM customers c
    WHERE c.deleted_at IS NULL
      AND ($10::uuid IS NULL OR c.group_id = $10::uuid)
)
SELECT
    (SELECT SUM(total_cents) FROM scoped
      WHERE ordered_at >= $1::date AND ordered_at < $2::date)::bigint AS revenue_current,
    (SELECT SUM(total_cents) FROM scoped
      WHERE ordered_at >= $3::date AND ordered_at < $4::date)::bigint AS revenue_mom,
    (SELECT SUM(total_cents) FROM scoped
      WHERE ordered_at >= $5::date AND ordered_at < $6::date)::bigint AS revenue_yoy,
    (SELECT COUNT(*) FROM scoped
      WHERE ordered_at >= $1::date AND ordered_at < $2::date)::bigint AS orders_current,
    (SELECT COUNT(*) FROM scoped
      WHERE ordered_at >= $3::date AND ordered_at < $4::date)::bigint AS orders_mom,
    (SELECT COUNT(*) FROM scoped
      WHERE ordered_at >= $5::date AND ordered_at < $6::date)::bigint AS orders_yoy,
    (SELECT COUNT(DISTINCT customer_id) FROM scoped
      WHERE ordered_at >= $1::date AND ordered_at < $2::date)::bigint AS buyers_current,
    (SELECT COUNT(DISTINCT customer_id) FROM scoped
      WHERE ordered_at >= $3::date AND ordered_at < $4::date)::bigint AS buyers_mom,
    (SELECT COUNT(DISTINCT customer_id) FROM scoped
      WHERE ordered_at >= $5::date AND ordered_at < $6::date)::bigint AS buyers_yoy,
    (SELECT COUNT(*) FROM scoped_cust
      WHERE created_at >= $1::date AND created_at < $2::date)::bigint AS new_customers_current,
    (SELECT COUNT(*) FROM scoped_cust
      WHERE created_at >= $3::date AND created_at < $4::date)::bigint AS new_customers_mom,
    (SELECT COUNT(*) FROM scoped_cust
      WHERE created_at >= $5::date AND created_at < $6::date)::bigint AS new_customers_yoy,
    (SELECT SUM(total_cents) FROM scoped
      WHERE ordered_at >= $7::date AND ordered_at < $8::date)::bigint AS revenue_ytd,
    (SELECT COUNT(*) FROM scoped
      WHERE ordered_at >= $7::date AND ordered_at < $8::date)::bigint AS orders_ytd,
    (SELECT COUNT(DISTINCT customer_id) FROM scoped
      WHERE ordered_at >= $7::date AND ordered_at < $8::date)::bigint AS buyers_ytd,
    (SELECT COUNT(*) FROM scoped_cust
      WHERE created_at >= $7::date AND created_at < $8::date)::bigint AS new_customers_ytd,
    -- Task 64 inputs (additive): total units sold per window, from order_items.
    -- SUM is NULL for an empty window, mirroring revenue_* so the caller can
    -- tell "no orders" apart from a real zero. Ratios (avg order spending,
    -- avg items per order, avg buyer spending) are derived server-side in
    -- services/api/analytics/periods.py from these + the revenue/orders/
    -- buyers figures above, so no existing column changes shape.
    (SELECT SUM(oi.qty) FROM orders o
      JOIN stores s ON s.id = o.store_id
      LEFT JOIN brands b ON b.id = s.brand_id
      LEFT JOIN groups g ON g.id = b.group_id
      JOIN order_items oi ON oi.order_id = o.id
      WHERE o.ordered_at >= $1::date AND o.ordered_at < $2::date
        AND ($9::uuid IS NULL OR b.id = $9::uuid)
        AND ($10::uuid IS NULL OR g.id = $10::uuid))::bigint AS items_current,
    (SELECT SUM(oi.qty) FROM orders o
      JOIN stores s ON s.id = o.store_id
      LEFT JOIN brands b ON b.id = s.brand_id
      LEFT JOIN groups g ON g.id = b.group_id
      JOIN order_items oi ON oi.order_id = o.id
      WHERE o.ordered_at >= $3::date AND o.ordered_at < $4::date
        AND ($9::uuid IS NULL OR b.id = $9::uuid)
        AND ($10::uuid IS NULL OR g.id = $10::uuid))::bigint AS items_mom,
    (SELECT SUM(oi.qty) FROM orders o
      JOIN stores s ON s.id = o.store_id
      LEFT JOIN brands b ON b.id = s.brand_id
      LEFT JOIN groups g ON g.id = b.group_id
      JOIN order_items oi ON oi.order_id = o.id
      WHERE o.ordered_at >= $5::date AND o.ordered_at < $6::date
        AND ($9::uuid IS NULL OR b.id = $9::uuid)
        AND ($10::uuid IS NULL OR g.id = $10::uuid))::bigint AS items_yoy,
    (SELECT SUM(oi.qty) FROM orders o
      JOIN stores s ON s.id = o.store_id
      LEFT JOIN brands b ON b.id = s.brand_id
      LEFT JOIN groups g ON g.id = b.group_id
      JOIN order_items oi ON oi.order_id = o.id
      WHERE o.ordered_at >= $7::date AND o.ordered_at < $8::date
        AND ($9::uuid IS NULL OR b.id = $9::uuid)
        AND ($10::uuid IS NULL OR g.id = $10::uuid))::bigint AS items_ytd;

-- name: monthly_series
-- params: months int, anchor date|None, brand uuid|None, group uuid|None
-- Revenue / orders / new customers per calendar month, anchored on the
-- caller's as-of month (LD5 `mode`: latest passes NULL -> the data's own
-- last month; current passes today -> the series runs to the current month
-- with trailing pre-data months zero-filled and the gap named by the
-- response's data_through marker) and zero-filled so the chart shape is
-- stable. A 13-month span lets the UI draw a full year of trend.
WITH scoped AS (
    SELECT o.ordered_at, o.total_cents
    FROM orders o
    JOIN stores s ON s.id = o.store_id
    LEFT JOIN brands b ON b.id = s.brand_id
    LEFT JOIN groups g ON g.id = b.group_id
    WHERE ($3::uuid IS NULL OR b.id = $3::uuid)
      AND ($4::uuid IS NULL OR g.id = $4::uuid)
), scoped_cust AS (
    SELECT c.created_at
    FROM customers c
    WHERE c.deleted_at IS NULL
      AND ($4::uuid IS NULL OR c.group_id = $4::uuid)
), anchor AS (
    SELECT date_trunc('month', COALESCE($2::date, (SELECT MAX(ordered_at) FROM scoped), now()))::date AS m0
), series AS (
    SELECT generate_series(a.m0 - make_interval(months => $1 - 1), a.m0, interval '1 month')::date AS month
    FROM anchor a
)
SELECT to_char(s.month, 'YYYY-MM') AS month,
       COALESCE((SELECT SUM(o.total_cents) FROM scoped o
                  WHERE date_trunc('month', o.ordered_at)::date = s.month), 0)::bigint AS revenue_cents,
       COALESCE((SELECT COUNT(*) FROM scoped o
                  WHERE date_trunc('month', o.ordered_at)::date = s.month), 0)::bigint AS orders,
       COALESCE((SELECT COUNT(*) FROM scoped_cust c
                  WHERE date_trunc('month', c.created_at)::date = s.month), 0)::bigint AS new_customers
FROM series s
ORDER BY s.month;

-- name: store_windows
-- params: cur_start date, cur_end date, mom_start date, mom_end date, yoy_start date, yoy_end date, ytd_start date, ytd_end date, brand uuid|None, group uuid|None
-- Revenue + orders per store over every window, with the store's brand
-- and group so the same result can be read as a store, brand or group
-- breakdown. Stores with no orders in any window still appear (LEFT JOIN) so
-- a business user can see the ones that sold nothing, which is itself the
-- actionable finding.
SELECT s.code AS store_code,
       s.name AS store_name,
       b.code AS brand_code,
       g.code AS group_code,
       COALESCE(SUM(o.total_cents) FILTER (WHERE o.ordered_at >= $1::date AND o.ordered_at < $2::date), 0)::bigint AS revenue_current,
       COALESCE(SUM(o.total_cents) FILTER (WHERE o.ordered_at >= $3::date AND o.ordered_at < $4::date), 0)::bigint AS revenue_mom,
       COALESCE(SUM(o.total_cents) FILTER (WHERE o.ordered_at >= $5::date AND o.ordered_at < $6::date), 0)::bigint AS revenue_yoy,
       COALESCE(SUM(o.total_cents) FILTER (WHERE o.ordered_at >= $7::date AND o.ordered_at < $8::date), 0)::bigint AS revenue_ytd,
       COUNT(o.id) FILTER (WHERE o.ordered_at >= $7::date AND o.ordered_at < $8::date)::bigint AS orders_ytd,
       COUNT(o.id) FILTER (WHERE o.ordered_at >= $1::date AND o.ordered_at < $2::date)::bigint AS orders_current,
       COUNT(o.id) FILTER (WHERE o.ordered_at >= $3::date AND o.ordered_at < $4::date)::bigint AS orders_mom,
       COUNT(o.id) FILTER (WHERE o.ordered_at >= $5::date AND o.ordered_at < $6::date)::bigint AS orders_yoy,
       COUNT(DISTINCT o.customer_id) FILTER (WHERE o.ordered_at >= $1::date AND o.ordered_at < $2::date)::bigint AS buyers_current
FROM stores s
LEFT JOIN brands b ON b.id = s.brand_id
LEFT JOIN groups g ON g.id = b.group_id
LEFT JOIN orders o ON o.store_id = s.id
     AND o.ordered_at >= LEAST($1::date, $3::date, $5::date, $7::date)
     AND o.ordered_at < GREATEST($2::date, $4::date, $6::date, $8::date)
WHERE ($9::uuid IS NULL OR b.id = $9::uuid)
  AND ($10::uuid IS NULL OR g.id = $10::uuid)
GROUP BY s.code, s.name, b.code, g.code
ORDER BY revenue_current DESC, s.code;

-- name: customer_cohort_windows
-- params: cur_start date, cur_end date, mom_start date, mom_end date, yoy_start date, yoy_end date, group uuid|None
-- The store's mirror image: orders/revenue grouped by the CUSTOMER's own
-- group, i.e. the "group" lens a business user asks for when the store is not
-- the unit of analysis. Customers carry no brand, so this query takes only
-- the group filter -- a brand-scoped session narrows via the store breakdown
-- instead.
SELECT g.code AS group_code,
       g.name AS group_name,
       COALESCE(SUM(o.total_cents) FILTER (WHERE o.ordered_at >= $1::date AND o.ordered_at < $2::date), 0)::bigint AS revenue_current,
       COALESCE(SUM(o.total_cents) FILTER (WHERE o.ordered_at >= $3::date AND o.ordered_at < $4::date), 0)::bigint AS revenue_mom,
       COALESCE(SUM(o.total_cents) FILTER (WHERE o.ordered_at >= $5::date AND o.ordered_at < $6::date), 0)::bigint AS revenue_yoy,
       COUNT(o.id) FILTER (WHERE o.ordered_at >= $1::date AND o.ordered_at < $2::date)::bigint AS orders_current,
       COUNT(DISTINCT o.customer_id) FILTER (WHERE o.ordered_at >= $1::date AND o.ordered_at < $2::date)::bigint AS buyers_current
FROM orders o
JOIN customers c ON c.id = o.customer_id AND c.deleted_at IS NULL
LEFT JOIN groups g ON g.id = c.group_id
WHERE o.ordered_at >= LEAST($1::date, $3::date, $5::date)
  AND o.ordered_at < GREATEST($2::date, $4::date, $6::date)
  AND ($7::uuid IS NULL OR g.id = $7::uuid)
GROUP BY g.code, g.name
ORDER BY revenue_current DESC;
