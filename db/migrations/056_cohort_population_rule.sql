-- migrate:up
-- 056: the cohort row names the population it describes (data D2, [ADDITIVE] analytics).
--
-- `cohort_snapshots` rows carried the cohort PERIOD (a column) and the
-- cohort's SIZE (a number), but not the RULE that places a customer in a
-- cohort: "the period of their first completed order, inside this row's
-- scope, on the +08 business calendar". A reader can see WHICH period a
-- cell is about and cannot say WHICH customers the cell is about without
-- reading the writer's DDL - and the scope lives in two more columns, so
-- the rule was the one thing left implicit.
--
-- The default is the writer's constant (`POPULATION_RULE` in
-- pipelines/snapshot.py) - the same string, byte for byte; a test asserts
-- the two are equal, so the row can never name a population the writer
-- does not measure. The writer writes the column explicitly on every
-- insert and keeps it on update.
--
-- Writer: COHORT_SNAPSHOT_UPSERT_SQL in pipelines/snapshot.py.

ALTER TABLE cohort_snapshots
  ADD COLUMN population_rule text NOT NULL DEFAULT
    'customers whose first completed order (business calendar, UTC+08) falls in the cohort period, within the scope of this row';

-- migrate:down
ALTER TABLE cohort_snapshots DROP COLUMN IF EXISTS population_rule;
