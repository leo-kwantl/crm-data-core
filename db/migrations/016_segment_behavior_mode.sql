-- migrate:up
-- 016: widen segment_runs.mode for the ml "behavior" segments mode
-- (category one-hot + log1p(qty) mean-pooled, z-scored, space
-- "behavior_mean_pool"). Additive: existing rows unaffected.

ALTER TABLE segment_runs DROP CONSTRAINT IF EXISTS segment_runs_mode_check;
ALTER TABLE segment_runs ADD CONSTRAINT segment_runs_mode_check
  CHECK (mode IN ('purchase', 'overall', 'behavior'));

-- migrate:down
ALTER TABLE segment_runs DROP CONSTRAINT IF EXISTS segment_runs_mode_check;
ALTER TABLE segment_runs ADD CONSTRAINT segment_runs_mode_check
  CHECK (mode IN ('purchase', 'overall'));
