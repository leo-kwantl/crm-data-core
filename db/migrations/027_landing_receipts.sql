-- migrate:up
-- 027: LD12 landing receipts (data, [ADDITIVE]).
--
-- One receipt per landed batch: file uploads (upload_job_id FK, one
-- receipt per job) OR vendor batch imports (vendor_batch_id external
-- key, no upload_job). Counts must reconcile with the source of
-- truth (upload_jobs counters / connector_sync_runs rows). contract
-- names the contracts.md section the batch was validated against
-- (e.g. 'orders', 'events', 'identity'). manifest_pipeline links the
-- scheduler manifest that drove the load (pipeline_manifests PK) —
-- NULL for manual/ad-hoc loads. Receipts are immutable
-- (REVOKE UPDATE/DELETE) like every other history table.

CREATE TABLE landing_receipts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  upload_job_id uuid NULL UNIQUE
    REFERENCES upload_jobs (id) ON DELETE RESTRICT,
  vendor_batch_id text NULL
    CHECK (vendor_batch_id IS NULL
           OR char_length(vendor_batch_id) BETWEEN 1 AND 128),
  source text NOT NULL CHECK (char_length(source) BETWEEN 1 AND 64),
  rows_seen integer NOT NULL CHECK (rows_seen >= 0),
  rows_loaded integer NOT NULL CHECK (rows_loaded >= 0),
  rows_quarantined integer NOT NULL DEFAULT 0
    CHECK (rows_quarantined >= 0),
  contract text NOT NULL CHECK (char_length(contract) BETWEEN 1 AND 64),
  manifest_pipeline text NULL
    REFERENCES pipeline_manifests (pipeline),
  received_at timestamptz NOT NULL DEFAULT now(),
  CHECK (rows_loaded + rows_quarantined <= rows_seen),
  CHECK ((upload_job_id IS NOT NULL)::int
         + (vendor_batch_id IS NOT NULL)::int = 1)
);

CREATE INDEX landing_receipts_vendor_idx
  ON landing_receipts (vendor_batch_id)
  WHERE vendor_batch_id IS NOT NULL;

REVOKE UPDATE, DELETE ON landing_receipts FROM PUBLIC;

-- migrate:down
DROP TABLE IF EXISTS landing_receipts;
