-- migrate:up
-- `clusters_skipped` says WHICH clusters a run explained nothing for; it has
-- never said WHY. ml's `explain_payload.py` distinguishes two facts that look
-- identical to a reader once the writer's summary is gone -- a cluster too
-- small to attribute ("too few members to attribute (needs >= 2)") and a
-- cluster that IS the cohort ("this cluster holds the whole cohort; nothing to
-- contrast") -- and both used to read as "the page dropped it".
--
-- Nullable with no default on purpose: a batch written before this column
-- reads NULL = "not recorded". An empty object must not be substituted, or
-- "nobody wrote the reasons" would render as "nothing was skipped".
ALTER TABLE explain_batches ADD COLUMN IF NOT EXISTS skipped_reasons jsonb NULL;

-- migrate:down
ALTER TABLE explain_batches DROP COLUMN IF EXISTS skipped_reasons;
