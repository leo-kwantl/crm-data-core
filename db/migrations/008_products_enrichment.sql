-- migrate:up
-- Products enrichment: merchandising columns + per-store inventory.
-- All additive / backfill-safe: new product columns NULL-able or DEFAULTed
-- (existing rows untouched, defaults fill in); inventory starts empty.
-- Fresh DBs get the full shape; demo enrichment rows live in db/seeds/demo.sql.

-- 1. Merchandising columns on products.
ALTER TABLE products ADD COLUMN IF NOT EXISTS description TEXT NULL;
ALTER TABLE products ADD COLUMN IF NOT EXISTS specs JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE products ADD COLUMN IF NOT EXISTS image_url TEXT NULL;
ALTER TABLE products ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true;

-- 2. Per-store stock levels. One row per (product, store); product delete
-- cascades, store delete is blocked while stock rows reference it.
CREATE TABLE IF NOT EXISTS inventory (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id uuid NOT NULL REFERENCES products (id) ON DELETE CASCADE,
  store_id uuid NOT NULL REFERENCES stores (id) ON DELETE RESTRICT,
  qty int NOT NULL DEFAULT 0 CHECK (qty >= 0),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (product_id, store_id)
);
CREATE INDEX IF NOT EXISTS inventory_product_idx ON inventory (product_id);
CREATE INDEX IF NOT EXISTS inventory_store_idx ON inventory (store_id);

-- migrate:down
DROP TABLE IF EXISTS inventory;
ALTER TABLE products DROP COLUMN IF EXISTS is_active;
ALTER TABLE products DROP COLUMN IF EXISTS image_url;
ALTER TABLE products DROP COLUMN IF EXISTS specs;
ALTER TABLE products DROP COLUMN IF EXISTS description;
