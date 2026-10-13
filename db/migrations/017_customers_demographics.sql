-- migrate:up
-- Real demographic fields on customers (contract change, additive only).
-- gender: closed vocabulary; date_of_birth: full years derived at read time
-- (age is NEVER stored, so it cannot go stale); district + address_line are
-- free text. All nullable: pre-existing rows keep honest NULLs.
-- PII: date_of_birth and address_line are PII - exact-match only, never in
-- GET query params (POST body, audited), same rule as phone/email.
ALTER TABLE customers ADD COLUMN gender TEXT NULL
  CHECK (gender IN ('female', 'male', 'other', 'undisclosed'));
ALTER TABLE customers ADD COLUMN date_of_birth DATE NULL
  CHECK (date_of_birth IS NULL OR date_of_birth <= CURRENT_DATE);
ALTER TABLE customers ADD COLUMN district TEXT NULL;
ALTER TABLE customers ADD COLUMN address_line TEXT NULL;
COMMENT ON COLUMN customers.gender IS 'self-declared gender vocabulary';
COMMENT ON COLUMN customers.date_of_birth IS 'PII: full years derived at read time via age()';
COMMENT ON COLUMN customers.district IS 'residential district, e.g. Central & Western';
COMMENT ON COLUMN customers.address_line IS 'PII: street address free text';

-- migrate:down
ALTER TABLE customers DROP COLUMN IF EXISTS address_line;
ALTER TABLE customers DROP COLUMN IF EXISTS district;
ALTER TABLE customers DROP COLUMN IF EXISTS date_of_birth;
ALTER TABLE customers DROP COLUMN IF EXISTS gender;
