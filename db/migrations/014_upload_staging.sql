-- migrate:up
-- Batch upload / ETL landing (phase 6, data owns): frontend file uploads
-- stage here, validate via pipelines/ingest normalize, quarantine rejects.
-- App owns the HTTP edge (POST /uploads -> row in upload_jobs); data owns
-- everything that lands rows. ML unaffected (new rows flow into snapshots).
--
-- Also fills two idempotency gaps found during review: orders has NO
-- external_order_id column and customer_events has NO source_event_id,
-- so the contracts.md dedup keys had no backing store until now.

-- ---- Idempotency columns (additive, nullable so existing rows pass) ----
ALTER TABLE orders ADD COLUMN IF NOT EXISTS source text NULL;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS external_order_id text NULL;
CREATE UNIQUE INDEX IF NOT EXISTS orders_source_ext_ux
  ON orders (source, external_order_id)
  WHERE source IS NOT NULL AND external_order_id IS NOT NULL;

ALTER TABLE customer_events ADD COLUMN IF NOT EXISTS source_event_id text NULL;
CREATE UNIQUE INDEX IF NOT EXISTS customer_events_source_ext_ux
  ON customer_events (source, source_event_id)
  WHERE source_event_id IS NOT NULL;

-- ---- Upload jobs: one row per file x scope ----
CREATE TABLE upload_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  filename text NOT NULL,
  file_hash text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('customers', 'orders', 'events', 'identity')),
  schema_version text NOT NULL DEFAULT 'v1',
  source text NOT NULL DEFAULT 'upload',
  status text NOT NULL DEFAULT 'received'
    CHECK (status IN ('received', 'staged', 'validated', 'committed',
                      'committed_with_rejects', 'failed')),
  total_rows integer NOT NULL DEFAULT 0 CHECK (total_rows >= 0),
  staged_rows integer NOT NULL DEFAULT 0 CHECK (staged_rows >= 0),
  committed_rows integer NOT NULL DEFAULT 0 CHECK (committed_rows >= 0),
  quarantined_rows integer NOT NULL DEFAULT 0 CHECK (quarantined_rows >= 0),
  error text NULL,
  created_by text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  brand_id uuid NULL REFERENCES brands (id),
  group_id uuid NULL REFERENCES groups (id)
);
-- Same file re-uploaded to the same scope = same job (no double-commit).
CREATE UNIQUE INDEX upload_jobs_hash_scope_ux
  ON upload_jobs (file_hash, kind,
    COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid));
CREATE INDEX upload_jobs_status_idx ON upload_jobs (status, created_at DESC);

-- ---- Staging: land-then-validate grain (one row per file line) ----
CREATE TABLE staging_rows (
  upload_job_id uuid NOT NULL REFERENCES upload_jobs (id) ON DELETE CASCADE,
  row_number integer NOT NULL CHECK (row_number >= 1),
  raw jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'ok', 'rejected')),
  reason text NULL,
  PRIMARY KEY (upload_job_id, row_number)
);

-- ---- Quarantine: rejects with (source, key, reason, raw), never dropped ----
CREATE TABLE quarantine_rows (
  upload_job_id uuid NOT NULL REFERENCES upload_jobs (id) ON DELETE CASCADE,
  row_number integer NOT NULL CHECK (row_number >= 1),
  source text NOT NULL DEFAULT 'upload',
  key text NULL,
  reason text NOT NULL,
  raw jsonb NOT NULL DEFAULT '{}'::jsonb,
  quarantined_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (upload_job_id, row_number)
);

REVOKE UPDATE, DELETE ON quarantine_rows FROM PUBLIC;

-- migrate:down
REVOKE ALL ON quarantine_rows FROM PUBLIC;
DROP TABLE IF EXISTS quarantine_rows;
DROP TABLE IF EXISTS staging_rows;
DROP TABLE IF EXISTS upload_jobs;
DROP INDEX IF EXISTS customer_events_source_ext_ux;
ALTER TABLE customer_events DROP COLUMN IF EXISTS source_event_id;
DROP INDEX IF EXISTS orders_source_ext_ux;
ALTER TABLE orders DROP COLUMN IF EXISTS external_order_id;
ALTER TABLE orders DROP COLUMN IF EXISTS source;
