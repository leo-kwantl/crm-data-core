-- migrate:up
-- Extensions are best-effort: present on managed PG (pilot/prod), but some
-- pgembed bundles lack the .control files. Guard via pg_available_extensions
-- so the same SQL runs everywhere. gen_random_uuid() works regardless.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pgcrypto') THEN
    EXECUTE 'CREATE EXTENSION IF NOT EXISTS "pgcrypto"';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm') THEN
    EXECUTE 'CREATE EXTENSION IF NOT EXISTS "pg_trgm"';
  END IF;
END $$;

CREATE TABLE customers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  member_id text NOT NULL UNIQUE CHECK (member_id ~ '^M-[0-9]{6}$'),
  full_name text NOT NULL CHECK (char_length(full_name) BETWEEN 1 AND 200),
  attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);
-- CONTRACT: pg_trgm GIN on customers(full_name) where the extension exists;
-- plain btree fallback otherwise (name search still works, less fuzzy).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
    EXECUTE 'CREATE INDEX IF NOT EXISTS customers_full_name_trgm ON customers USING gin (full_name gin_trgm_ops)';
  ELSE
    EXECUTE 'CREATE INDEX IF NOT EXISTS customers_full_name_idx ON customers (full_name)';
  END IF;
END $$;
CREATE INDEX customers_attributes_gin ON customers USING gin (attributes);
CREATE INDEX customers_deleted_at_idx ON customers (deleted_at) WHERE deleted_at IS NULL;

CREATE TABLE customer_contacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id uuid NOT NULL REFERENCES customers (id),
  kind text NOT NULL CHECK (kind IN ('phone', 'email')),
  value_normalized text NOT NULL,
  value_display text NOT NULL,
  is_primary boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kind, value_normalized)
);
CREATE INDEX customer_contacts_customer_idx ON customer_contacts (customer_id);

-- migrate:down
DROP INDEX IF EXISTS customers_full_name_trgm;
DROP INDEX IF EXISTS customers_full_name_idx;
DROP TABLE IF EXISTS customer_contacts;
DROP TABLE IF EXISTS customers;
