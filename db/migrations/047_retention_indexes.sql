-- migrate:up
-- Retention (pipelines/retention.py, 365 days) deletes by write time on five
-- tables that no index covered, so each age-based delete would be a sequential
-- scan on the very tables that grow (cohort_snapshots wrote 11,622 rows in one
-- weekly run; backtest_outcomes 3,756 in one). Small today, 18 GB at 100x --
-- which is exactly where a seq scan stops being free.
--
-- Indexes only: this migration declares no table, column or function, so the
-- declared-object drift guard cannot see it (documented blind spot of
-- test_no_unrecorded_migration_is_already_live) -- its ledger row is the only
-- evidence it ran.
CREATE INDEX IF NOT EXISTS explain_batches_created_at_idx
  ON explain_batches (created_at);
CREATE INDEX IF NOT EXISTS rec_batches_created_at_idx
  ON rec_batches (created_at);
CREATE INDEX IF NOT EXISTS cohort_snapshots_created_at_idx
  ON cohort_snapshots (created_at);
CREATE INDEX IF NOT EXISTS backtest_outcomes_created_at_idx
  ON backtest_outcomes (created_at);
CREATE INDEX IF NOT EXISTS pipeline_runs_created_at_idx
  ON pipeline_runs (created_at);

-- migrate:down
DROP INDEX IF EXISTS pipeline_runs_created_at_idx;
DROP INDEX IF EXISTS backtest_outcomes_created_at_idx;
DROP INDEX IF EXISTS cohort_snapshots_created_at_idx;
DROP INDEX IF EXISTS rec_batches_created_at_idx;
DROP INDEX IF EXISTS explain_batches_created_at_idx;
