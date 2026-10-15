-- migrate:up
-- 029: LM2 cluster-vs-population comparison matrix (data, [ADDITIVE]
-- analytics, joint w/ ml).
--
-- Data lands the table FIRST; ml's matrix writer writes rows matching
-- this grain. One row per (segment_run x cluster x feature x layout):
-- per-feature cluster mean/median vs population mean/median + diff +
-- pct_diff (NULL when the population median is 0 — no division
-- invented). layout 'wide' = one row per feature (matrix cells);
-- 'long' = UNPIVOTED stat rows for UI tables. Runs die -> their
-- matrix dies (CASCADE, same as 021 explain rows). Immutable
-- (REVOKE UPDATE/DELETE).

CREATE TABLE cluster_feature_matrix (
  segment_run_id uuid NOT NULL
    REFERENCES segment_runs (id) ON DELETE CASCADE,
  cluster integer NOT NULL CHECK (cluster >= 0),
  feature text NOT NULL
    CHECK (char_length(feature) BETWEEN 1 AND 128),
  cluster_mean double precision NOT NULL,
  cluster_median double precision NOT NULL,
  population_mean double precision NOT NULL,
  population_median double precision NOT NULL,
  diff double precision NOT NULL,
  pct_diff double precision NULL,
  layout text NOT NULL DEFAULT 'wide'
    CHECK (layout IN ('wide', 'long')),
  PRIMARY KEY (segment_run_id, cluster, feature, layout)
);

CREATE INDEX cluster_matrix_run_idx
  ON cluster_feature_matrix (segment_run_id, layout);

REVOKE UPDATE, DELETE ON cluster_feature_matrix FROM PUBLIC;

-- migrate:down
DROP TABLE IF EXISTS cluster_feature_matrix;
