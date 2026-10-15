-- migrate:up
-- 029: LM2 assoc-rule holdout eval (data, [ADDITIVE] analytics, joint w/ ml).
--
-- Data lands the table FIRST; ml's eval engine writes rows that match
-- this grain. One row per (batch x eval_set x metric): the holdout
-- proof behind every assoc batch. Metric rows (not columns) so new
-- metrics never need DDL. n_holdout + evaluated_at ride each row so a
-- metric is never read without its denominator. Batches die ->
-- their eval dies (CASCADE, same as assoc_rules in 018). Immutable
-- (REVOKE UPDATE/DELETE).

CREATE TABLE assoc_rules_eval (
  batch_id uuid NOT NULL
    REFERENCES assoc_batches (id) ON DELETE CASCADE,
  eval_set text NOT NULL DEFAULT 'holdout'
    CHECK (char_length(eval_set) BETWEEN 1 AND 32),
  metric text NOT NULL CHECK (char_length(metric) BETWEEN 1 AND 64),
  value double precision NOT NULL,
  n_holdout integer NOT NULL CHECK (n_holdout >= 0),
  evaluated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (batch_id, eval_set, metric)
);

REVOKE UPDATE, DELETE ON assoc_rules_eval FROM PUBLIC;

-- migrate:down
DROP TABLE IF EXISTS assoc_rules_eval;
