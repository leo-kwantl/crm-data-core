-- migrate:up
-- Repair: the demo cluster recorded 014_upload_staging as applied while the
-- current text of that file had never run there. 014 gained `orders.source`
-- and `orders_source_ext_ux` on 2026-10-15, the cluster's data directory
-- predates that, and both dbmate and scripts/bootstrap_db.py skip a version
-- already present in schema_migrations -- so editing an applied migration
-- landed on nobody, and pipelines/apply.py failed at its first statement
-- (`column "source" does not exist`).
--
-- Re-stating the two statements under a NEW version is the only way to reach
-- an environment that already ran 014. IF NOT EXISTS keeps a fresh database a
-- no-op, and the partial index only covers rows that carry both keys, so the
-- pre-feature rows (source IS NULL) never conflict.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS source text NULL;
CREATE UNIQUE INDEX IF NOT EXISTS orders_source_ext_ux
  ON orders (source, external_order_id)
  WHERE source IS NOT NULL AND external_order_id IS NOT NULL;

-- migrate:down
-- Intentionally empty: 014 owns both objects. Dropping them here would break
-- a database that built them from 014's own text.
