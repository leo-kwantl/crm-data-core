-- 040_explain_batch_coverage.sql
--
-- How much of a segment run a SHAP batch actually explained. ml's
-- `store_explain.explain_matrix` already returns `clusters_explained` and
-- `clusters_skipped` (the cluster NUMBERS, not just a count -- "segment 3 was
-- not explained" is the renderable fact) and nothing persisted them, so the
-- app could not tell a full batch from one that silently dropped clusters.
-- crm-ml asked for this DDL (DM 2026-10-19); they bind both columns in
-- `BATCH_INSERT_SQL` and this file is the storage half.
--
-- Deliberately NULLABLE with no default. A default of 0 / '{}' would make
-- every batch written before this migration read as "explained nothing, skipped
-- nothing", which is a false claim rather than a missing one; NULL is the
-- convention the neighbouring ml stores already use (`seg_surrogate_fits.accuracy`
-- NULL = fidelity not recorded, rendered as such by the app). New rows always
-- carry both, so a query that needs certainty can filter `IS NOT NULL`.

-- migrate:up
ALTER TABLE explain_batches ADD COLUMN IF NOT EXISTS clusters_explained integer;
ALTER TABLE explain_batches ADD COLUMN IF NOT EXISTS clusters_skipped integer[];
COMMENT ON COLUMN explain_batches.clusters_explained IS
  'how many clusters this batch explained; NULL = written before 040, not recorded';
COMMENT ON COLUMN explain_batches.clusters_skipped IS
  'cluster numbers this batch could not explain (too few members / one cluster); NULL = not recorded';

-- migrate:down
ALTER TABLE explain_batches DROP COLUMN IF EXISTS clusters_skipped;
ALTER TABLE explain_batches DROP COLUMN IF EXISTS clusters_explained;
