-- migrate:up
-- 032: long-layout comparison matrix (LM2, [ADDITIVE] analytics,
-- joint w/ ml).
--
-- Answers ml's grain question on 029: the wide table's PK
-- (run, cluster, feature, layout) cannot hold melted stat rows, and
-- stat-name-in-feature would pollute the feature namespace that
-- serving parses — so long gets its own table with its own clean
-- grain instead of PK surgery on 029. One row per
-- (segment_run x cluster x feature x stat); stat is one of the six
-- wide value columns by name; value is NULL-able (pct_diff NULL on
-- zero population, same rule as wide). Runs die → long rows die
-- (CASCADE, same as 029). Immutable (REVOKE UPDATE/DELETE).
-- ml: write one row per stat per feature per cluster; serving reads
-- one run via LONG_MATRIX_FOR_RUN_SQL.

CREATE TABLE cluster_feature_matrix_long (
  segment_run_id uuid NOT NULL
    REFERENCES segment_runs (id) ON DELETE CASCADE,
  cluster integer NOT NULL CHECK (cluster >= 0),
  feature text NOT NULL
    CHECK (char_length(feature) BETWEEN 1 AND 256),
  stat text NOT NULL
    CHECK (stat IN ('cluster_mean', 'cluster_median',
                    'population_mean', 'population_median',
                    'diff', 'pct_diff')),
  value double precision NULL,
  PRIMARY KEY (segment_run_id, cluster, feature, stat)
);

REVOKE UPDATE, DELETE ON cluster_feature_matrix_long FROM PUBLIC;

-- migrate:down
DROP TABLE IF EXISTS cluster_feature_matrix_long;
