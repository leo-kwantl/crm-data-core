-- migrate:up
-- New-domains batch: stores contact columns, membership tiers, campaigns.
-- All additive / backfill-safe: new columns NULL-able, new tables empty on
-- existing DBs. Fresh DBs get the full shape; seed rows live in db/seeds/demo.sql.

-- 1. Stores: contact columns (code/name untouched).
ALTER TABLE stores ADD COLUMN IF NOT EXISTS address TEXT NULL;
ALTER TABLE stores ADD COLUMN IF NOT EXISTS phone TEXT NULL;

-- 2. Membership tiers (MVP: manual assignment only, no points engine).
-- customers.tier_code references the human-stable code, not the uuid, so
-- tier renames/splits never rewrite customer rows.
CREATE TABLE IF NOT EXISTS membership_tiers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE, -- e.g. SILVER/GOLD/PLATINUM (upper-case by convention)
  name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
  rank int NOT NULL CHECK (rank >= 0), -- higher = better; display order
  benefits_text text NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE customers ADD COLUMN IF NOT EXISTS tier_code TEXT NULL
  REFERENCES membership_tiers (code);
CREATE INDEX IF NOT EXISTS customers_tier_code_idx
  ON customers (tier_code) WHERE tier_code IS NOT NULL;

-- 3. Campaigns (promotion-first) + member assignments.
CREATE TABLE IF NOT EXISTS campaigns (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE, -- e.g. MID-AUTUMN-20PCT
  name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
  kind text NOT NULL DEFAULT 'promotion',
  status text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'active', 'ended')),
  starts_at timestamptz NULL,
  ends_at timestamptz NULL,
  config jsonb NOT NULL DEFAULT '{}'::jsonb, -- e.g. {"discount_pct": 20}
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS campaigns_status_idx ON campaigns (status);

CREATE TABLE IF NOT EXISTS campaign_members (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id uuid NOT NULL REFERENCES campaigns (id) ON DELETE CASCADE,
  customer_id uuid NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
  assigned_by text NOT NULL DEFAULT '',
  assigned_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (campaign_id, customer_id)
);
CREATE INDEX IF NOT EXISTS campaign_members_campaign_idx
  ON campaign_members (campaign_id, assigned_at DESC);

-- migrate:down
DROP TABLE IF EXISTS campaign_members;
DROP TABLE IF EXISTS campaigns;
DROP INDEX IF EXISTS customers_tier_code_idx;
ALTER TABLE customers DROP COLUMN IF EXISTS tier_code;
DROP TABLE IF EXISTS membership_tiers;
ALTER TABLE stores DROP COLUMN IF EXISTS phone;
ALTER TABLE stores DROP COLUMN IF EXISTS address;
