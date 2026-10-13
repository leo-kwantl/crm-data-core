-- migrate:up
-- Analytics phase 1: RFM snapshots + per-customer scores.
-- Snapshots are immutable (no UPDATE/DELETE grants for app roles, same
-- pattern as customer_events / consent_events / customer_access_logs).

CREATE TABLE rfm_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  mode text NOT NULL CHECK (mode IN ('rolling_365', 'year_to_date', 'custom')),
  window_from date NOT NULL,
  window_to date NOT NULL,
  scored_at timestamptz NOT NULL DEFAULT now(),
  rules_version text NOT NULL DEFAULT 'v1',
  method text NOT NULL DEFAULT 'quintile',
  customer_count integer NOT NULL DEFAULT 0 CHECK (customer_count >= 0),
  created_by text NULL
);
CREATE INDEX rfm_snapshots_mode_idx ON rfm_snapshots (mode, scored_at DESC);

CREATE TABLE rfm_customer_scores (
  snapshot_id uuid NOT NULL REFERENCES rfm_snapshots (id) ON DELETE CASCADE,
  customer_id uuid NOT NULL REFERENCES customers (id),
  r smallint NOT NULL CHECK (r BETWEEN 1 AND 5),
  f smallint NOT NULL CHECK (f BETWEEN 1 AND 5),
  m smallint NOT NULL CHECK (m BETWEEN 1 AND 5),
  segment text NOT NULL,
  recency_days integer NOT NULL CHECK (recency_days >= 0),
  frequency integer NOT NULL CHECK (frequency >= 1),
  monetary_cents bigint NOT NULL CHECK (monetary_cents >= 0),
  PRIMARY KEY (snapshot_id, customer_id)
);
CREATE INDEX rfm_scores_snapshot_segment_idx
  ON rfm_customer_scores (snapshot_id, segment);

-- Append-only: no UPDATE/DELETE grants for app roles; owner keeps full rights.
REVOKE UPDATE, DELETE ON rfm_snapshots, rfm_customer_scores FROM PUBLIC;

-- migrate:down
DROP TABLE IF EXISTS rfm_customer_scores;
DROP TABLE IF EXISTS rfm_snapshots;
