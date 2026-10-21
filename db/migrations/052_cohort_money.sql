-- migrate:up
-- 052: cohort retained money (data D2, [ADDITIVE] analytics).
--
-- The cohort table counted the customers a cohort brought back but not
-- the money they brought back, so the reader's ARPU had no column to
-- divide by. `retained_cents` is the money cohort members spent in each
-- period, summed from the completed orders of that period, and written
-- by the same writer as the counts
-- (COHORT_SNAPSHOT_UPSERT_SQL in pipelines/snapshot.py).
--
-- Money is integer cents, never a float. NOT NULL DEFAULT 0: a row
-- written before this migration reads 0 cents, which means "not
-- measured" - a reader must say "no money yet", never "$0.00". Since the
-- writer re-measures its cells (a re-run is no longer a no-op - see
-- 020_cohort_snapshots.sql and pipelines/contracts.md "020"), the next
-- run fills the money in the historical rows and no separate backfill is
-- needed.

ALTER TABLE cohort_snapshots
  ADD COLUMN retained_cents bigint NOT NULL DEFAULT 0
  CHECK (retained_cents >= 0);

-- migrate:down
ALTER TABLE cohort_snapshots DROP COLUMN IF EXISTS retained_cents;
