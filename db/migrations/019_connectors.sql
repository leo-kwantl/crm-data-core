-- migrate:up
-- 019: source connector framework (data D1, [BREAKING ingest]).
--
-- source_types are equals: shopify | csv | excel | delta_lake | rest_api.
-- woocommerce is RESERVED (spec pending) — deliberately NOT in the CHECK;
-- widening the CHECK is the additive path when its spec lands.
--
-- connectors: one row per registered source. config holds NON-SECRET
-- params only (validated at registration by
-- pipelines/ingest/connectors.py validate_connector); credentials live in
-- env/secret-manager and secret_ref names the env var. The DB NEVER
-- stores a plaintext secret; rotation = update env, no DDL.
-- connector_sync_runs: per-connector+entity run history with row counts +
-- error. NOT an extension of 015 pipeline_runs: different grain (sync run
-- vs scheduled job) and different readers (connector health vs cron
-- orchestration). pipeline_runs stays job-grain, untouched.

CREATE TABLE connectors (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_type text NOT NULL CHECK (source_type IN
    ('shopify', 'csv', 'excel', 'delta_lake', 'rest_api')),
  name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 128),
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  secret_ref text NULL CHECK (secret_ref IS NULL
    OR (char_length(secret_ref) BETWEEN 1 AND 128)),
  enabled boolean NOT NULL DEFAULT true,
  created_by text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX connectors_type_name_ux
  ON connectors (source_type, name);

CREATE TABLE connector_sync_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  connector_id uuid NOT NULL REFERENCES connectors (id) ON DELETE CASCADE,
  entity text NOT NULL CHECK (entity IN
    ('customers', 'orders', 'events', 'identity', 'products')),
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
  rows_seen integer NOT NULL DEFAULT 0 CHECK (rows_seen >= 0),
  rows_ok integer NOT NULL DEFAULT 0 CHECK (rows_ok >= 0),
  rows_rejected integer NOT NULL DEFAULT 0 CHECK (rows_rejected >= 0),
  error text NULL,
  started_at timestamptz NULL,
  finished_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  brand_id uuid NULL REFERENCES brands (id),
  group_id uuid NULL REFERENCES groups (id)
);
CREATE INDEX connector_sync_runs_connector_idx
  ON connector_sync_runs (connector_id, started_at DESC);
CREATE INDEX connector_sync_runs_scope_idx
  ON connector_sync_runs (brand_id, group_id);

-- migrate:down
DROP TABLE IF EXISTS connector_sync_runs;
DROP TABLE IF EXISTS connectors;
