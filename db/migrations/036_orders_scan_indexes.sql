-- 036_orders_scan_indexes.sql
--
-- The KPI day window (`orders_day`, `revenue_day_cents`), the active-30d
-- window and the cohort pass all scan `orders` with `status = 'completed'`
-- plus an `ordered_at` range or GROUP BY. Only `orders_pkey`,
-- `orders_customer_idx (customer_id, ordered_at DESC)` and the partial
-- `orders_source_ext_ux` existed, so the KPI day counts were a seq scan
-- over the whole table with ~99% of rows filtered out (review §4.3-6).
--
-- Partial on `status = 'completed'` so the predicate is answered from the
-- index itself: heap fetches 0, one day's window read as an index range.
--
-- Deliberately NOT added: a second `(customer_id, ordered_at)` partial
-- index for the cohort GROUP BY. Measured on the demo cluster it is a
-- loss today -- all 8,686 orders are 'completed', so the partial index
-- carries no selectivity and the forced index-only plan costs 12.5 ms
-- against a 2.0 ms seq scan over 134 pages. It pays only once abandoned
-- carts make 'completed' a minority; add it with that measurement then.

-- migrate:up
CREATE INDEX IF NOT EXISTS orders_completed_ordered_at_idx
  ON orders (ordered_at)
  WHERE status = 'completed';

-- migrate:down
DROP INDEX IF EXISTS orders_completed_ordered_at_idx;
