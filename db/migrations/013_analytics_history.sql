-- migrate:up
-- Analytics history (phase 5, data owns): daily snapshots so movement,
-- pct-change, and model-training lineage can be computed across days.
--
-- Why: rfm_snapshots + rec_batches are the ONLY persisted runs today.
-- CLV/churn/segments are live-compute (same call, same answer) — no
-- history, so no trend, no pct-change, no training-set versioning.
-- This migration adds the missing immutable run tables plus one small
-- KPI grain (one row per day x scope) for cheap trend/pct queries.
--
-- Conventions (match 010/011/012): immutable history — no UPDATE/DELETE
-- grants for app roles; brand_id/group_id scope tags (NULL = group-wide
-- or legacy); idempotent daily writes via COALESCE-based UNIQUE indexes
-- (plain UNIQUE allows duplicate NULLs, so plain UNIQUE is NOT enough).

-- ---- CLV snapshots (mirrors rfm_snapshots shape) ----
CREATE TABLE clv_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  as_of date NOT NULL,
  horizon_days integer NOT NULL CHECK (horizon_days BETWEEN 1 AND 730),
  frequency_fit jsonb NOT NULL DEFAULT '{}'::jsonb,
  spend_fit jsonb NOT NULL DEFAULT '{}'::jsonb,
  customer_count integer NOT NULL DEFAULT 0 CHECK (customer_count >= 0),
  created_by text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  brand_id uuid NULL REFERENCES brands (id),
  group_id uuid NULL REFERENCES groups (id)
);
CREATE UNIQUE INDEX clv_snapshots_day_scope_ux
  ON clv_snapshots (as_of, horizon_days,
    COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid));
CREATE INDEX clv_snapshots_scope_idx ON clv_snapshots (brand_id, group_id);

CREATE TABLE clv_customer_scores (
  snapshot_id uuid NOT NULL REFERENCES clv_snapshots (id) ON DELETE CASCADE,
  customer_id uuid NOT NULL REFERENCES customers (id),
  repeat_tx integer NOT NULL DEFAULT 0 CHECK (repeat_tx >= 0),
  p_alive double precision NOT NULL CHECK (p_alive >= 0 AND p_alive <= 1),
  expected_purchases double precision NOT NULL CHECK (expected_purchases >= 0),
  avg_order_value_cents double precision NOT NULL CHECK (avg_order_value_cents >= 0),
  clv_cents double precision NOT NULL CHECK (clv_cents >= 0),
  value_tier text NOT NULL DEFAULT 'low' CHECK (value_tier IN ('low', 'mid', 'high')),
  PRIMARY KEY (snapshot_id, customer_id)
);
CREATE INDEX clv_scores_snapshot_tier_idx
  ON clv_customer_scores (snapshot_id, value_tier);

-- ---- Churn snapshots (BG/NBD p_alive per customer per day) ----
CREATE TABLE churn_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  as_of date NOT NULL,
  activity text NOT NULL CHECK (activity IN ('orders', 'all')),
  horizon_days integer NOT NULL CHECK (horizon_days BETWEEN 1 AND 730),
  fit jsonb NOT NULL DEFAULT '{}'::jsonb,
  customer_count integer NOT NULL DEFAULT 0 CHECK (customer_count >= 0),
  created_by text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  brand_id uuid NULL REFERENCES brands (id),
  group_id uuid NULL REFERENCES groups (id)
);
CREATE UNIQUE INDEX churn_snapshots_day_scope_ux
  ON churn_snapshots (as_of, activity, horizon_days,
    COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid));
CREATE INDEX churn_snapshots_scope_idx ON churn_snapshots (brand_id, group_id);

CREATE TABLE churn_customer_scores (
  snapshot_id uuid NOT NULL REFERENCES churn_snapshots (id) ON DELETE CASCADE,
  customer_id uuid NOT NULL REFERENCES customers (id),
  repeat_tx integer NOT NULL DEFAULT 0 CHECK (repeat_tx >= 0),
  recency_days double precision NOT NULL DEFAULT 0 CHECK (recency_days >= 0),
  p_alive double precision NOT NULL CHECK (p_alive >= 0 AND p_alive <= 1),
  expected_purchases double precision NOT NULL CHECK (expected_purchases >= 0),
  band text NOT NULL DEFAULT 'active' CHECK (band IN ('at-risk', 'cooling', 'active')),
  PRIMARY KEY (snapshot_id, customer_id)
);
CREATE INDEX churn_scores_snapshot_band_idx
  ON churn_customer_scores (snapshot_id, band);

-- ---- KPI daily grain: one row per day x scope ----
-- Cumulative totals (for level charts) + day deltas (for pct-change) +
-- 30d actives (for movement). Small: 1 row/day/scope, cheap to query.
CREATE TABLE kpi_daily_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  snapshot_date date NOT NULL,
  customers_total integer NOT NULL DEFAULT 0 CHECK (customers_total >= 0),
  orders_total bigint NOT NULL DEFAULT 0 CHECK (orders_total >= 0),
  revenue_cents_total bigint NOT NULL DEFAULT 0 CHECK (revenue_cents_total >= 0),
  orders_day integer NOT NULL DEFAULT 0 CHECK (orders_day >= 0),
  revenue_day_cents bigint NOT NULL DEFAULT 0 CHECK (revenue_day_cents >= 0),
  new_customers_day integer NOT NULL DEFAULT 0 CHECK (new_customers_day >= 0),
  active_30d integer NOT NULL DEFAULT 0 CHECK (active_30d >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  brand_id uuid NULL REFERENCES brands (id),
  group_id uuid NULL REFERENCES groups (id)
);
CREATE UNIQUE INDEX kpi_daily_day_scope_ux
  ON kpi_daily_snapshots (snapshot_date,
    COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid));
CREATE INDEX kpi_daily_scope_date_idx
  ON kpi_daily_snapshots (brand_id, group_id, snapshot_date DESC);

-- ---- Model training runs: which snapshot trained which model ----
CREATE TABLE model_training_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model text NOT NULL CHECK (model IN ('rfm', 'clv', 'churn', 'segment', 'recs')),
  snapshot_id uuid NULL,
  dataset_from date NULL,
  dataset_to date NULL,
  params jsonb NOT NULL DEFAULT '{}'::jsonb,
  metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
  trained_at timestamptz NOT NULL DEFAULT now(),
  created_by text NULL,
  brand_id uuid NULL REFERENCES brands (id),
  group_id uuid NULL REFERENCES groups (id)
);
CREATE INDEX model_training_runs_model_idx
  ON model_training_runs (model, trained_at DESC);

-- Immutable history: same convention as rfm_snapshots / rec_batches /
-- customer_events (owner keeps full rights; app roles get SELECT+INSERT).
REVOKE UPDATE, DELETE ON clv_snapshots, clv_customer_scores,
  churn_snapshots, churn_customer_scores,
  kpi_daily_snapshots, model_training_runs FROM PUBLIC;

-- migrate:down
REVOKE ALL ON model_training_runs, kpi_daily_snapshots,
  churn_customer_scores, churn_snapshots,
  clv_customer_scores, clv_snapshots FROM PUBLIC;
DROP TABLE IF EXISTS model_training_runs;
DROP TABLE IF EXISTS kpi_daily_snapshots;
DROP TABLE IF EXISTS churn_customer_scores;
DROP TABLE IF EXISTS churn_snapshots;
DROP TABLE IF EXISTS clv_customer_scores;
DROP TABLE IF EXISTS clv_snapshots;
