-- migrate:up
-- Quarantine was a dead end a reader could see but not interpret: 014 revoked
-- UPDATE/DELETE (append-only, correct), the reason was free text, and a row
-- fixed and re-applied left its old reject looking live forever. Three
-- additive changes, no new table:
--
--   quarantine_rows.code         stable token from pipelines/ingest/errors.py,
--                                so a run summary can say {code: count}
--   quarantine_rows.resolved_at  set by the applier when the same (source, key)
--                                lands cleanly afterwards -- the record is
--                                retired, never edited or deleted
--   upload_jobs.quarantine_reasons  the job's own {code: count} map, so a run
--                                that dropped rows cannot read as a clean pass
ALTER TABLE quarantine_rows ADD COLUMN IF NOT EXISTS code text NULL;
ALTER TABLE quarantine_rows ADD COLUMN IF NOT EXISTS resolved_at timestamptz NULL;

-- Still append-only: exactly one column is writable, and only to retire a
-- record. No row can be altered or removed.
GRANT UPDATE (resolved_at) ON quarantine_rows TO PUBLIC;

-- The retry lookup: "is there an open reject for this (source, key)?".
CREATE INDEX IF NOT EXISTS quarantine_rows_open_idx
  ON quarantine_rows (source, key) WHERE resolved_at IS NULL;

ALTER TABLE upload_jobs
  ADD COLUMN IF NOT EXISTS quarantine_reasons jsonb NOT NULL DEFAULT '{}'::jsonb;

-- migrate:down
ALTER TABLE upload_jobs DROP COLUMN IF EXISTS quarantine_reasons;
DROP INDEX IF EXISTS quarantine_rows_open_idx;
REVOKE UPDATE (resolved_at) ON quarantine_rows FROM PUBLIC;
ALTER TABLE quarantine_rows DROP COLUMN IF EXISTS resolved_at;
ALTER TABLE quarantine_rows DROP COLUMN IF EXISTS code;
