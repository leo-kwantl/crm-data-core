-- migrate:up
-- Brand/group scoping for analytics runs. rfm_snapshots and rec_batches were
-- created unscoped (phase 1/4): a brand-scoped session computing a run would
-- store brand-filtered scores, but listing/reading runs had no scope check,
-- so a brand-scoped session could read another brand's run. Tag every run
-- with the scope it was computed under and filter reads by session scope.
--
-- NULL brand_id = computed group-wide (manager/admin) or legacy pre-scope
-- run: readable group-wide per the NULL-brand convention in deps.check_brand.
-- group_id = owning group (resolved from the brand, else the session group);
-- NULL group_id = HQ/legacy, visible to brand-less sessions only alongside
-- their own scope (see repo guard logic).

ALTER TABLE rfm_snapshots
  ADD COLUMN IF NOT EXISTS brand_id uuid NULL REFERENCES brands (id),
  ADD COLUMN IF NOT EXISTS group_id uuid NULL REFERENCES groups (id);
CREATE INDEX IF NOT EXISTS rfm_snapshots_scope_idx
  ON rfm_snapshots (brand_id, group_id);

ALTER TABLE rec_batches
  ADD COLUMN IF NOT EXISTS brand_id uuid NULL REFERENCES brands (id),
  ADD COLUMN IF NOT EXISTS group_id uuid NULL REFERENCES groups (id);
CREATE INDEX IF NOT EXISTS rec_batches_scope_idx
  ON rec_batches (brand_id, group_id);

-- migrate:down
DROP INDEX IF EXISTS rec_batches_scope_idx;
DROP INDEX IF EXISTS rfm_snapshots_scope_idx;
ALTER TABLE rec_batches DROP COLUMN IF EXISTS group_id;
ALTER TABLE rec_batches DROP COLUMN IF EXISTS brand_id;
ALTER TABLE rfm_snapshots DROP COLUMN IF EXISTS group_id;
ALTER TABLE rfm_snapshots DROP COLUMN IF EXISTS brand_id;
