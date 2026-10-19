-- 042_training_run_models.sql
--
-- Widen `model_training_runs.model` to the two models ml records quality rows
-- for. Requested by crm-ml (manager's trust-layer item): this table is how
-- "which snapshot trained which model" is answered, and `forecast` and `assoc`
-- rows could not be written at all (`CheckViolationError`), so two models had
-- no lineage and their writers were left unwired rather than fail a tick.
--
-- Widening only: every value that inserted before still does, so no existing
-- row or writer can be rejected by this change. `013_analytics_history.sql`
-- declares the original five values; `ml_engine/training_runs.py` `MODELS`
-- grows to match, and any reader's list has to move with the pair -- a value
-- the table accepts but no reader knows is invisible lineage, and a value the
-- table rejects is a failed tick.

-- migrate:up
ALTER TABLE model_training_runs
  DROP CONSTRAINT model_training_runs_model_check;
ALTER TABLE model_training_runs
  ADD CONSTRAINT model_training_runs_model_check
  CHECK (model IN ('rfm', 'clv', 'churn', 'segment', 'recs',
                   'forecast', 'assoc'));

-- migrate:down
-- Narrowing: fails if `forecast`/`assoc` rows exist (nothing is deleted here).
ALTER TABLE model_training_runs
  DROP CONSTRAINT model_training_runs_model_check;
ALTER TABLE model_training_runs
  ADD CONSTRAINT model_training_runs_model_check
  CHECK (model IN ('rfm', 'clv', 'churn', 'segment', 'recs'));
