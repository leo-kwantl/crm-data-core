-- migrate:up
-- 025: LD2 per-connector schedule + sync checkpoint (data, [ADDITIVE]).
--
-- connectors gains schedule_cron (NULL = manual-only; validated 5-field
-- cron by pipelines.ingest.connectors.validate_schedule_cron) and a
-- health checkpoint (last_sync_at/last_sync_status) updated by the sync
-- worker via note_sync_result. Purely additive: no new table, no
-- backfill (NULLs = never scheduled / never synced).

ALTER TABLE connectors
  ADD COLUMN schedule_cron text NULL
    CHECK (schedule_cron IS NULL
           OR char_length(schedule_cron) BETWEEN 1 AND 64),
  ADD COLUMN last_sync_at timestamptz NULL,
  ADD COLUMN last_sync_status text NULL
    CHECK (last_sync_status IS NULL
           OR last_sync_status IN
              ('queued', 'running', 'succeeded', 'failed'));

-- migrate:down
ALTER TABLE connectors
  DROP COLUMN IF EXISTS last_sync_status,
  DROP COLUMN IF EXISTS last_sync_at,
  DROP COLUMN IF EXISTS schedule_cron;
