-- migrate:up
-- 021: SHAP cluster-explainer result tables (ml M2, data-owned storage).
--
-- explain_batches: one row per run of ml_jobs/run_explain_batch.py
-- (segment_run_id required; mode/k/seed stamp the explained segment run;
-- top_n/feature_count/customer_count/cluster_count mirror the job output).
-- explain_rows: top-N features per (batch, cluster) by mean|SHAP| with
-- mean/max/min summaries. PNGs live in MLflow only — this table holds
-- JSON-able rows; app serves latest-run summaries, URIs never blobs.
--
-- SQL shape is owned by ml_engine/store_explain.py — keep column
-- names/types in sync; any signature/schema change must be announced
-- [BREAKING] per protocol. UNIQUE(batch, cluster, rank) guards
-- double-write; reruns create a new batch.

CREATE TABLE explain_batches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  segment_run_id uuid NOT NULL REFERENCES segment_runs (id)
    ON DELETE CASCADE,
  mode text NOT NULL,
  k integer NOT NULL CHECK (k >= 2),
  seed integer NOT NULL DEFAULT 0,
  top_n integer NOT NULL CHECK (top_n >= 1),
  feature_count integer NOT NULL DEFAULT 0 CHECK (feature_count >= 0),
  customer_count integer NOT NULL DEFAULT 0 CHECK (customer_count >= 0),
  cluster_count integer NOT NULL DEFAULT 0 CHECK (cluster_count >= 0),
  created_by text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  brand_id uuid NULL REFERENCES brands (id),
  group_id uuid NULL REFERENCES groups (id)
);
CREATE INDEX explain_batches_segment_run_idx
  ON explain_batches (segment_run_id);
CREATE INDEX explain_batches_scope_idx
  ON explain_batches (brand_id, group_id);

CREATE TABLE explain_rows (
  id bigserial PRIMARY KEY,
  batch_id uuid NOT NULL REFERENCES explain_batches (id) ON DELETE CASCADE,
  cluster integer NOT NULL CHECK (cluster >= 0),
  rank integer NOT NULL CHECK (rank >= 1),
  feature text NOT NULL CHECK (char_length(feature) BETWEEN 1 AND 128),
  mean_abs_shap double precision NOT NULL CHECK (mean_abs_shap >= 0),
  mean_shap double precision NOT NULL,
  min_shap double precision NOT NULL,
  max_shap double precision NOT NULL,
  CHECK (min_shap <= max_shap),
  UNIQUE (batch_id, cluster, rank)
);
CREATE INDEX explain_rows_batch_idx ON explain_rows (batch_id);

-- migrate:down
DROP TABLE IF EXISTS explain_rows;
DROP TABLE IF EXISTS explain_batches;
