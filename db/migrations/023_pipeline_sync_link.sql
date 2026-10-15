-- migrate:up
-- 023: LD3 sync->pipeline lineage (data, [ADDITIVE]).
--
-- pipeline_runs gains a NULLABLE sync_run_id FK to 019 connector_sync_runs.
-- Grain is unchanged (still one row per pipeline/job/as_of/scope); the link
-- is set only when a scheduled job runs downstream of a connector sync, so
-- connector health traces to derived refreshes. NULL = time-based tick
-- with no feeding sync (the common case until LD2 per-connector cron
-- writes sync rows). ON DELETE SET NULL: run history survives sync-row
-- pruning.

ALTER TABLE pipeline_runs
  ADD COLUMN sync_run_id uuid NULL
    REFERENCES connector_sync_runs (id) ON DELETE SET NULL;

-- migrate:down
ALTER TABLE pipeline_runs DROP COLUMN IF EXISTS sync_run_id;
