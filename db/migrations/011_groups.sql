-- migrate:up
-- Multi-brand group hierarchy: groups -> brands -> stores.
-- Single-brand clients = one group + one brand (invisible to operators).
-- Loyalty stays brand-scoped NOW; group-level pooling is RESERVED only
-- (membership_tiers.pool_scope, unused by any logic — see comment below).
-- Customers are shared within a group (customers.group_id) with NO linkage
-- across unrelated clients (hard silo at the group level); linkable-by-design
-- via customers.linked_customer_id (nullable self-FK, no trigger, no
-- auto-merge/matching logic anywhere — pure reservation).
-- Consents gain brand_id so new writes are scoped per brand.

-- Default demo holder rows (fixed UUIDs, shared with db/seeds/demo.sql):
-- group 60000000-...-000001 (code DEMO), brand 60100000-...-000001 (code DEMO).
CREATE TABLE IF NOT EXISTS groups (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE,
  name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS brands (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id uuid NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
  code text NOT NULL UNIQUE,
  name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS brands_group_id_idx ON brands (group_id);

INSERT INTO groups (id, code, name) VALUES
  ('60000000-0000-0000-0000-000000000001', 'DEMO', 'Demo Group')
ON CONFLICT (id) DO NOTHING;

INSERT INTO brands (id, group_id, code, name) VALUES
  ('60100000-0000-0000-0000-000000000001',
   '60000000-0000-0000-0000-000000000001', 'DEMO', 'Demo Flagship Brand')
ON CONFLICT (id) DO NOTHING;

-- Stores hang under a brand. Backfill every brand-less store (seed UUIDs and
-- any operator-created rows) into the default brand, then enforce NOT NULL:
-- the hierarchy line is stores.brand_id, no legacy NULLs remain.
ALTER TABLE stores ADD COLUMN IF NOT EXISTS brand_id uuid NULL
  REFERENCES brands (id);
UPDATE stores SET brand_id = '60100000-0000-0000-0000-000000000001'
WHERE brand_id IS NULL;
ALTER TABLE stores ALTER COLUMN brand_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS stores_brand_id_idx ON stores (brand_id);

-- Staff brand scope: brand_id NULL = group-wide (manager/admin only, by
-- convention — new sa rows should always carry a brand; brand-less sa keeps
-- legacy group-wide reads so pre-brand deployments keep working).
-- Backfill from the staff member's home store brand where one is set.
ALTER TABLE staff ADD COLUMN IF NOT EXISTS brand_id uuid NULL
  REFERENCES brands (id);
UPDATE staff SET brand_id = stores.brand_id
FROM stores WHERE stores.id = staff.store_id
  AND staff.brand_id IS NULL AND staff.store_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS staff_brand_id_idx ON staff (brand_id)
  WHERE brand_id IS NOT NULL;

-- Campaigns are brand-scoped on write (repos filter lists by session brand;
-- cross-brand reads are 403). NULL = legacy/pre-brand row: visible to
-- group-wide roles (admin/manager) only, never to brand-scoped sa.
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS brand_id uuid NULL
  REFERENCES brands (id);
CREATE INDEX IF NOT EXISTS campaigns_brand_id_idx ON campaigns (brand_id)
  WHERE brand_id IS NOT NULL;

-- Membership tiers: brand_id NULL = global catalogue row (visible to every
-- brand — the seed tiers stay global); non-NULL = brand-specific tier.
-- pool_scope is the RESERVED group-pooling extension point: nullable TEXT,
-- written by nothing, read by nothing. When group-level point pooling lands,
-- scope values (e.g. 'group') and earn/redeem rules attach here — until then
-- loyalty derives live per brand from orders (see repos/tiers.py).
ALTER TABLE membership_tiers ADD COLUMN IF NOT EXISTS brand_id uuid NULL
  REFERENCES brands (id);
ALTER TABLE membership_tiers ADD COLUMN IF NOT EXISTS pool_scope TEXT NULL;
COMMENT ON COLUMN membership_tiers.pool_scope IS
  'RESERVED: group-level loyalty pooling scope (unused — no logic reads or writes this yet)';
CREATE INDEX IF NOT EXISTS membership_tiers_brand_id_idx
  ON membership_tiers (brand_id) WHERE brand_id IS NOT NULL;

-- Customers belong to a group (shared within it). linked_customer_id is a
-- pure reservation: nullable self-FK with no trigger/constraint/matching —
-- cross-client linkage stays impossible until an explicit link flow lands.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS group_id uuid NULL
  REFERENCES groups (id);
ALTER TABLE customers ADD COLUMN IF NOT EXISTS linked_customer_id uuid NULL
  REFERENCES customers (id);
COMMENT ON COLUMN customers.linked_customer_id IS
  'RESERVED: explicit cross-record link (no auto-merge/matching logic writes this)';
CREATE INDEX IF NOT EXISTS customers_group_id_idx ON customers (group_id)
  WHERE group_id IS NOT NULL;

-- Consents: new writes carry the acting brand (repos/consents.py); legacy
-- rows stay NULL (readable group-wide). No backfill here — the demo seed
-- stamps its own rows; operator data keeps NULL = pre-brand.
ALTER TABLE consents ADD COLUMN IF NOT EXISTS brand_id uuid NULL
  REFERENCES brands (id);
CREATE INDEX IF NOT EXISTS consents_brand_id_idx ON consents (brand_id)
  WHERE brand_id IS NOT NULL;

-- migrate:down
DROP INDEX IF EXISTS consents_brand_id_idx;
ALTER TABLE consents DROP COLUMN IF EXISTS brand_id;
DROP INDEX IF EXISTS customers_group_id_idx;
ALTER TABLE customers DROP COLUMN IF EXISTS linked_customer_id;
ALTER TABLE customers DROP COLUMN IF EXISTS group_id;
ALTER TABLE membership_tiers DROP COLUMN IF EXISTS pool_scope;
DROP INDEX IF EXISTS membership_tiers_brand_id_idx;
ALTER TABLE membership_tiers DROP COLUMN IF EXISTS brand_id;
DROP INDEX IF EXISTS campaigns_brand_id_idx;
ALTER TABLE campaigns DROP COLUMN IF EXISTS brand_id;
DROP INDEX IF EXISTS staff_brand_id_idx;
ALTER TABLE staff DROP COLUMN IF EXISTS brand_id;
DROP INDEX IF EXISTS stores_brand_id_idx;
ALTER TABLE stores ALTER COLUMN brand_id DROP NOT NULL;
ALTER TABLE stores DROP COLUMN IF EXISTS brand_id;
DROP INDEX IF EXISTS brands_group_id_idx;
DROP TABLE IF EXISTS brands;
DROP TABLE IF EXISTS groups;
