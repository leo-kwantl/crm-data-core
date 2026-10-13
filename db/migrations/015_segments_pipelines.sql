-- migrate:up
-- 015: segment runs + pipeline orchestration state (ml part 2, data-owned).
--
-- segment_runs / segment_assignments mirror the 013 convention (run header
-- + COALESCE-scope UNIQUE + (run, customer) scores PK). Centroids live in
-- TRANSFORMED space (purchase: L2-normalized taste rows; overall: z-scored)
-- with an ordered `features` array so online scoring can align columns:
-- purchase dims = category list in pivot-column order; overall dims =
-- [r_score, f_score, m_score, tenure_days, avg_gap_days, breadth,
--  avg_basket, tier_rank]. `inertia` = sum of squared euclidean distances
-- to assigned centroids in that space (best of n_init); run-quality metric
-- only, NOT comparable across k/mode — document, don't gate.
-- pipeline_runs records scheduled job executions; pipeline_manifests stores
-- the validated manifest YAML (edits go through the app config API, which
-- validates via ml; the runner trusts stored rows).

CREATE TABLE segment_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  as_of date NOT NULL,
  mode text NOT NULL CHECK (mode IN ('purchase', 'overall')),
  k integer NOT NULL CHECK (k >= 2),
  seed integer NOT NULL DEFAULT 0,
  centroids jsonb NOT NULL DEFAULT '[]'::jsonb,
  features jsonb NOT NULL DEFAULT '[]'::jsonb,
  inertia double precision NULL CHECK (inertia IS NULL OR inertia >= 0),
  customer_count integer NOT NULL DEFAULT 0 CHECK (customer_count >= 0),
  rules_version text NOT NULL DEFAULT 'v1',
  created_by text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  brand_id uuid NULL REFERENCES brands (id),
  group_id uuid NULL REFERENCES groups (id)
);
CREATE UNIQUE INDEX segment_runs_day_scope_ux
  ON segment_runs (as_of, mode, k,
    COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid));
CREATE INDEX segment_runs_scope_idx ON segment_runs (brand_id, group_id);

-- customer_id only (013 convention): member stays out of score tables,
-- display joins resolve it. label is the RULES_VERSION display name.
CREATE TABLE segment_assignments (
  run_id uuid NOT NULL REFERENCES segment_runs (id) ON DELETE CASCADE,
  customer_id uuid NOT NULL REFERENCES customers (id),
  segment integer NOT NULL CHECK (segment >= 0),
  label text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 64),
  PRIMARY KEY (run_id, customer_id)
);
CREATE INDEX segment_assignments_run_segment_idx
  ON segment_assignments (run_id, segment);

-- ---- Pipeline execution state (job grain) ----
CREATE TABLE pipeline_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pipeline text NOT NULL CHECK (char_length(pipeline) BETWEEN 1 AND 64),
  job text NOT NULL CHECK (char_length(job) BETWEEN 1 AND 64),
  as_of date NOT NULL,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'skipped')),
  run_id uuid NULL,
  upstream jsonb NOT NULL DEFAULT '{}'::jsonb,
  error text NULL,
  started_at timestamptz NULL,
  finished_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  brand_id uuid NULL REFERENCES brands (id),
  group_id uuid NULL REFERENCES groups (id)
);
CREATE UNIQUE INDEX pipeline_runs_job_scope_ux
  ON pipeline_runs (pipeline, job, as_of,
    COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid));
CREATE INDEX pipeline_runs_pipeline_idx
  ON pipeline_runs (pipeline, as_of DESC);

-- ---- Validated manifest store (one row per pipeline) ----
CREATE TABLE pipeline_manifests (
  pipeline text PRIMARY KEY CHECK (char_length(pipeline) BETWEEN 1 AND 64),
  yaml text NOT NULL,
  version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
  updated_by text NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---- v1 manifest seeds: content-verbatim split of the agreed
-- ml_jobs/pipelines.example.yaml (one row per pipeline; later edits go
-- through the app config API, never migrations). Re-serialized per
-- pipeline (round-trip verified identical); ON CONFLICT keeps admin
-- edits across re-applies.
INSERT INTO pipeline_manifests (pipeline, yaml, version, updated_by)
VALUES ('nightly', $$version: 1
pipelines:
  nightly:
    schedule: 0 1 * * *
    scopes:
      brands:
      - '*'
      groups:
      - '*'
    jobs:
      apply:
        engine: upload_apply
        params:
          strategy: validated_only
      rfm:
        engine: rfm_snapshot
        depends_on:
        - apply
        params:
          mode: rolling_365
      churn:
        engine: churn_snapshot
        depends_on:
        - apply
        params:
          activity: orders
          horizon_days: 90
      clv:
        engine: clv_snapshot
        depends_on:
        - apply
        params:
          horizon_days: 90
$$, 1, 'seed-015')
ON CONFLICT (pipeline) DO NOTHING;
INSERT INTO pipeline_manifests (pipeline, yaml, version, updated_by)
VALUES ('weekly', $$version: 1
pipelines:
  weekly:
    schedule: 0 2 * * 0
    scopes:
      brands:
      - '*'
      groups:
      - '*'
    jobs:
      segments_purchase:
        engine: segments
        params:
          mode: purchase
          k: 6
      segments_overall:
        engine: segments
        depends_on:
        - segments_purchase
        params:
          mode: overall
          k: 6
      recs:
        engine: recs_batch
        params:
          top_n: 10
          min_support: 2
          min_confidence: 0.2
$$, 1, 'seed-015')
ON CONFLICT (pipeline) DO NOTHING;

-- migrate:down
DROP TABLE IF EXISTS pipeline_manifests;
DROP TABLE IF EXISTS pipeline_runs;
DROP TABLE IF EXISTS segment_assignments;
DROP TABLE IF EXISTS segment_runs;
