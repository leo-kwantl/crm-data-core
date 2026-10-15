-- migrate:up
-- 022: demand-forecast snapshot tables (ml M3, data-owned storage).
--
-- forecast_batches: one row per run of ml_jobs/run_forecast_snapshot.py
-- (as_of/method/horizon_days/lookback_weeks/category_count/
-- skipped_count/scope). Method `trend_seasonal_naive` (least-squares
-- trend over last 12 wks + additive weekly-season phase + 80% residual
-- interval, floored at 0); defaults horizon 28d / lookback 52wks /
-- min-history 8 nonzero wks. Thin categories land in skipped_count
-- (listed in the job output, never silent).
-- forecast_rows: one row per (batch, category, Monday-week period_start)
-- with predicted_qty + 80% interval (lower_qty/upper_qty).
--
-- Idempotent per (as_of, method, horizon, lookback, scope): the UNIQUE
-- below carries method (different method = different forecast; same
-- inputs re-run = no-op). Rows carry UNIQUE(batch, category,
-- period_start) against double-write.
-- SQL shape is owned by ml_engine/store_forecast.py — keep column
-- names/types in sync; any signature/schema change must be announced
-- [BREAKING] per protocol.

CREATE TABLE forecast_batches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  as_of date NOT NULL,
  method text NOT NULL CHECK (char_length(method) BETWEEN 1 AND 64),
  horizon_days integer NOT NULL CHECK (horizon_days >= 1),
  lookback_weeks integer NOT NULL CHECK (lookback_weeks >= 1),
  category_count integer NOT NULL DEFAULT 0 CHECK (category_count >= 0),
  skipped_count integer NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
  created_by text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  brand_id uuid NULL REFERENCES brands (id),
  group_id uuid NULL REFERENCES groups (id)
);
CREATE UNIQUE INDEX forecast_batches_grain_ux
  ON forecast_batches (as_of, method, horizon_days, lookback_weeks,
    COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid));
CREATE INDEX forecast_batches_scope_idx
  ON forecast_batches (brand_id, group_id);

CREATE TABLE forecast_rows (
  id bigserial PRIMARY KEY,
  batch_id uuid NOT NULL REFERENCES forecast_batches (id) ON DELETE CASCADE,
  category text NOT NULL CHECK (char_length(category) BETWEEN 1 AND 128),
  period_start date NOT NULL,
  predicted_qty double precision NOT NULL CHECK (predicted_qty >= 0),
  lower_qty double precision NOT NULL CHECK (lower_qty >= 0),
  upper_qty double precision NOT NULL CHECK (upper_qty >= 0),
  CHECK (lower_qty <= predicted_qty AND predicted_qty <= upper_qty),
  UNIQUE (batch_id, category, period_start)
);
CREATE INDEX forecast_rows_batch_idx ON forecast_rows (batch_id);

-- migrate:down
DROP TABLE IF EXISTS forecast_rows;
DROP TABLE IF EXISTS forecast_batches;
