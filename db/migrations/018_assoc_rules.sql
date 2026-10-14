-- migrate:up
-- 018: category-association result tables (ml M1, data-owned storage).
--
-- assoc_batches: one row per run of ml_engine.association (FPGrowth-style,
-- category grain). params (incl. min_support_used), customer_count,
-- category_count, rule_count mirror ml Jobs output. FK brand/group via scope.
-- assoc_rules: child rows per (batch_id, antecedents, consequents) with
-- support/confidence/lift. Cascade-delete when a batch is removed.
--
-- SQL shape is owned by ml_engine/store_assoc.py — keep column names/types
-- in sync; any signature/schema change must be announced [BREAKING ingest].

CREATE TABLE assoc_batches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  params jsonb NOT NULL DEFAULT '{}'::jsonb,
  customer_count integer NOT NULL DEFAULT 0 CHECK (customer_count >= 0),
  category_count integer NOT NULL DEFAULT 0 CHECK (category_count >= 0),
  rule_count integer NOT NULL DEFAULT 0 CHECK (rule_count >= 0),
  min_support_used integer NULL CHECK (min_support_used IS NULL OR min_support_used >= 1),
  created_by text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  brand_id uuid NULL REFERENCES brands (id),
  group_id uuid NULL REFERENCES groups (id)
);
CREATE INDEX assoc_batches_scope_idx ON assoc_batches (brand_id, group_id);
CREATE INDEX assoc_batches_created_idx ON assoc_batches (created_at DESC);

CREATE TABLE assoc_rules (
  id bigserial PRIMARY KEY,
  batch_id uuid NOT NULL REFERENCES assoc_batches (id) ON DELETE CASCADE,
  antecedents jsonb NOT NULL DEFAULT '[]'::jsonb,
  consequents jsonb NOT NULL DEFAULT '[]'::jsonb,
  support double precision NOT NULL CHECK (support >= 0 AND support <= 1),
  confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  lift double precision NOT NULL CHECK (lift >= 0)
);
CREATE INDEX assoc_rules_batch_idx ON assoc_rules (batch_id);

-- migrate:down
DROP TABLE IF EXISTS assoc_rules;
DROP TABLE IF EXISTS assoc_batches;
