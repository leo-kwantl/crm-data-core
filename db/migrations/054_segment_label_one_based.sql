-- migrate:up
-- 054: the segment label is 1-based, and the DDL says so.
--
-- 015_segments_pipelines.sql declared
--   segment integer NOT NULL CHECK (segment >= 0)
-- while the writer (ml) stores 1..k (segment == cluster + 1). The column
-- therefore PERMITTED 0, and a reader that bound `segment - 1` into a
-- members query served each segment's previous segment's customers (app
-- P0, 2026-10-20). The constraint is what let the wrong value in; this
-- file closes it.
--
-- The census that justifies the tightening: on the live dev cluster,
-- 20,519 assignments across 40 stored runs, every run labelling exactly
-- 1..k, and 0 rows with segment < 1. The rewrite touches no row.
--
-- The same file states the base of every reader-facing integer label
-- whose base was only discoverable in another repo's code. Each base
-- below was MEASURED on the live dev cluster, not assumed:
--
--   segment_assignments.segment          1-based   1..k per run
--   rec_items.rank                       1-based   1..10
--   membership_tiers.rank                1-based   1..3 stored
--   rfm_customer_scores.r / f / m        1-based   1..5 quintile
--   explain_rows.cluster                 0-based   0..k-1
--   cluster_feature_matrix.cluster       0-based   0..k-1
--   cluster_feature_matrix_long.cluster  0-based   0..k-1
--
-- Two families, two bases, on purpose: the segment label is what a
-- reader shows (1..k), the cluster index is what the model fitted
-- (0..k-1), and the label is the cluster plus one. A reader that mixes
-- the two is the bug this file exists to make impossible to repeat.
--
-- membership_tiers.rank is NOT tightened here: app writes that column
-- (services/api/repos/tiers.py), so its base is a client convention and
-- the DDL must not forbid what a client may send. Only the base is
-- stated, and the base is measured, not guessed.

ALTER TABLE segment_assignments
  DROP CONSTRAINT IF EXISTS segment_assignments_segment_check;
ALTER TABLE segment_assignments
  ADD CONSTRAINT segment_assignments_segment_check CHECK (segment >= 1);

COMMENT ON COLUMN segment_assignments.segment IS
  '1-based segment label; == ml''s cluster + 1; reader-facing tabs are 1..k';
COMMENT ON COLUMN rec_items.rank IS
  '1-based rank of the recommendation within the batch for that customer (1 = first served)';
COMMENT ON COLUMN membership_tiers.rank IS
  'display order, higher = better; 1-based in every stored row, but app writes this column, so the base is a client convention, not a constraint';
COMMENT ON COLUMN rfm_customer_scores.r IS
  '1-based quintile (1..5): 1 = the least recent (the most days since purchase), 5 = the most recent';
COMMENT ON COLUMN rfm_customer_scores.f IS
  '1-based quintile (1..5): 1 = the fewest orders, 5 = the most';
COMMENT ON COLUMN rfm_customer_scores.m IS
  '1-based quintile (1..5): 1 = the smallest spend, 5 = the largest';
COMMENT ON COLUMN explain_rows.cluster IS
  '0-based cluster index (0..k-1); the segment label of the same run is this + 1';
COMMENT ON COLUMN cluster_feature_matrix.cluster IS
  '0-based cluster index (0..k-1)';
COMMENT ON COLUMN cluster_feature_matrix_long.cluster IS
  '0-based cluster index (0..k-1)';

-- migrate:down
ALTER TABLE segment_assignments
  DROP CONSTRAINT IF EXISTS segment_assignments_segment_check;
ALTER TABLE segment_assignments
  ADD CONSTRAINT segment_assignments_segment_check CHECK (segment >= 0);
COMMENT ON COLUMN segment_assignments.segment IS NULL;
COMMENT ON COLUMN rec_items.rank IS NULL;
COMMENT ON COLUMN membership_tiers.rank IS NULL;
COMMENT ON COLUMN rfm_customer_scores.r IS NULL;
COMMENT ON COLUMN rfm_customer_scores.f IS NULL;
COMMENT ON COLUMN rfm_customer_scores.m IS NULL;
COMMENT ON COLUMN explain_rows.cluster IS NULL;
COMMENT ON COLUMN cluster_feature_matrix.cluster IS NULL;
COMMENT ON COLUMN cluster_feature_matrix_long.cluster IS NULL;
