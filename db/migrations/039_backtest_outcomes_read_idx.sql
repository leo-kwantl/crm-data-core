-- 039_backtest_outcomes_read_idx.sql
--
-- 037 gave the table an identity (the window unique index) and a tenant scope
-- (backtest_outcomes_scope_idx), but no index leads with the columns a
-- per-window read filters on: `model, anchor_as_of, holdout_days`. ml writes
-- and (per §5.3-1) reads one window at a time to score a run, and the unique
-- index cannot serve that shape -- its leading terms are the COALESCE'd
-- brand/group pair, so a query that does not name them scans the heap.
--
-- Measured on the live cluster (3,756 rows = 4 windows x 939 customers,
-- 2026-10-19), planner free to choose:
--   SELECT ... WHERE model=$1 AND anchor_as_of=$2 AND holdout_days=$3
--     before: Seq Scan, 0.575 ms, 2,817 rows removed by filter, 62 buffers
--     after:  Bitmap Index Scan, 0.288 ms, 16 heap blocks
--   SELECT ... WHERE customer_id=$1   (a customer's history across windows)
--     before: Seq Scan, 0.256 ms, 3,752 rows removed by filter
--     after:  Bitmap Index Scan, 0.074 ms, 4 heap blocks
-- Cost at this size: 200 kB + 72 kB of index, and both grow one row per
-- customer per window with the table.
--
-- Refused, not overlooked: an aggregate over a whole window
-- (`WHERE brand_id IS NULL AND model=$1 AND anchor_as_of=$2`, half the table)
-- still seq-scans at 0.50 ms vs 0.45 ms -- the same measurement that refused
-- the cohort index in 036, and the reason neither new index leads with
-- brand_id.

-- migrate:up
CREATE INDEX IF NOT EXISTS backtest_outcomes_window_read_idx
    ON backtest_outcomes (model, anchor_as_of, holdout_days, customer_id);

CREATE INDEX IF NOT EXISTS backtest_outcomes_customer_idx
    ON backtest_outcomes (customer_id);

-- migrate:down
DROP INDEX IF EXISTS backtest_outcomes_customer_idx;
DROP INDEX IF EXISTS backtest_outcomes_window_read_idx;
