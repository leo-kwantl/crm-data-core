-- migrate:up
-- External member id synced from the client's POS (editable, nullable, unique).
-- NULLs are unrestricted (Postgres UNIQUE allows multiple NULLs); only
-- non-null values must be unique across customers.
ALTER TABLE customers ADD COLUMN external_member_id TEXT NULL UNIQUE;
COMMENT ON COLUMN customers.external_member_id IS 'client POS id';
CREATE INDEX IF NOT EXISTS customers_external_member_id_idx
  ON customers (external_member_id) WHERE external_member_id IS NOT NULL;

-- migrate:down
DROP INDEX IF EXISTS customers_external_member_id_idx;
ALTER TABLE customers DROP COLUMN IF EXISTS external_member_id;
